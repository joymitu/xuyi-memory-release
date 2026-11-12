"""记忆强度：重要程度 × 保持率，每条事件一个 0–1 的值。

派生数据：可销毁重建（像倒排索引），不碰原话。
只有「用户自己又提起同一件事」才加强；被召回、被注入、助手自己说的话都不算。
被更正掉的旧说法强度为 0。
"""
from __future__ import annotations

from datetime import datetime, timezone

SCOPE_PREFIX = "qq:c2c:"

IMPORTANCE = {
    "name": 1.0,
    "diet": 1.0,
    "preference": 0.8,
    "appointment": 0.8,
    "openloop": 0.8,
    "emotion": 0.6,
    "other": 0.3,
}
LONG_TERM = ("name", "diet", "preference", "appointment")
HALF_LIFE_DAYS = (7, 30, 180)      # 只提过 1 天 / 2 天 / 3 天及以上
LONG_TERM_FLOOR = 0.5
PINNED_IMPORTANCE = 1.0
FAMILY_ORDER = ("name", "preference", "appointment", "openloop", "emotion", "diet")


def _ensure(store):
    db = store.db
    db.execute("""CREATE TABLE IF NOT EXISTS strength_group(
        scope TEXT NOT NULL, gid INTEGER NOT NULL, family TEXT NOT NULL, rep TEXT NOT NULL,
        days INTEGER NOT NULL DEFAULT 0, last_at TEXT NOT NULL,
        PRIMARY KEY(scope, gid))""")
    db.execute("""CREATE TABLE IF NOT EXISTS memory_strength(
        scope TEXT NOT NULL, event_id TEXT NOT NULL, gid INTEGER,
        importance REAL NOT NULL, retention REAL NOT NULL, strength REAL NOT NULL,
        half_life REAL NOT NULL, PRIMARY KEY(scope, event_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS strength_days(
        scope TEXT NOT NULL, gid INTEGER NOT NULL, day TEXT NOT NULL,
        PRIMARY KEY(scope, gid, day))""")
    db.execute("""CREATE TABLE IF NOT EXISTS memory_overrides(
        scope TEXT NOT NULL, event_id TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
        muted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        PRIMARY KEY(scope, event_id))""")
    # 0.9.7 起：「这条记错了」——和用户说「别记了」一样，检索与主动面都当它不存在，原文不删，可恢复。
    columns = {row[1] for row in db.execute("PRAGMA table_info(memory_overrides)")}
    if "wrong" not in columns:
        db.execute("ALTER TABLE memory_overrides ADD COLUMN wrong INTEGER NOT NULL DEFAULT 0")
    db.commit()


EMOTION_WORDS = __import__("re").compile(r"难过|开心|高兴|伤心|害怕|生气|焦虑|好累|累死|烦死|孤单|委屈|想哭")
DIET_WORDS = __import__("re").compile(r"过敏|忌口|乳糖不耐|不能吃|不能喝|不吃|不喝")


def family_of(text):
    """这一段话属于哪个重点家族；都不是就是 other。

    家族自己的 covers() 只认很窄的句式（"我今天很难过" 认不出），所以显式情绪/忌口再用词表兜一层——
    这两个直接关系到"敏感类永不主动出现"，不能漏。
    """
    from .highlights import FAMILIES
    try:
        for name, family in zip(FAMILY_ORDER, FAMILIES):
            if family.relevant(text):
                return name
    except Exception:
        pass
    if EMOTION_WORDS.search(text or ""):
        return "emotion"
    if DIET_WORDS.search(text or ""):
        return "diet"
    return "other"


def _bigrams(text):
    norm = "".join(ch for ch in text if ch.isalnum())
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _similar(first, second):
    one, two = _bigrams(first), _bigrams(second)
    union = one | two
    return len(one & two) / len(union) if union else 0.0


def _day(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().date()
    except (AttributeError, ValueError):
        return None


def _superseded(store, scope):
    try:
        rows = store.db.execute(
            "SELECT supersedes_id FROM supersedes_map WHERE scope=?", (scope,)).fetchall()
    except Exception:
        return set()
    return {row[0] for row in rows}


def _override(store, scope, event_id):
    row = store.db.execute(
        "SELECT pinned, muted FROM memory_overrides WHERE scope=? AND event_id=?",
        (scope, event_id)).fetchone()
    return (bool(row[0]), bool(row[1])) if row else (False, False)


def _retention(last_at, half_life, now):
    try:
        moment = datetime.fromisoformat(last_at.replace("Z", "+00:00")).astimezone()
    except (AttributeError, ValueError):
        return 0.0
    age = max(0, int((now - moment).total_seconds() // 86400))
    return 0.5 ** (age / half_life) if half_life > 0 else 0.0


def _write(store, scope, event_id, gid, family, half_life, last_at, now, superseded):
    pinned, _muted = _override(store, scope, event_id)
    if pinned:
        importance, retention = PINNED_IMPORTANCE, 1.0
    else:
        importance = IMPORTANCE.get(family, 0.3)
        retention = _retention(last_at, half_life, now)
        if family in LONG_TERM:
            retention = max(retention, LONG_TERM_FLOOR)
    strength = 0.0 if event_id in superseded else importance * retention
    store.db.execute(
        "INSERT OR REPLACE INTO memory_strength(scope,event_id,gid,importance,retention,strength,half_life)"
        " VALUES(?,?,?,?,?,?,?)",
        (scope, event_id, gid, importance, retention, strength, half_life))


def rebuild(store, scopes=None, now=None):
    """全量重建（删了再建，结果必须一字不差）。"""
    _ensure(store)
    if scopes is None:
        scopes = [row[0] for row in store.db.execute(
            "SELECT DISTINCT scope FROM events WHERE scope LIKE ?", ("qq:%",))]
    now = now or datetime.now().astimezone()
    for scope in scopes:
        store.db.execute("DELETE FROM memory_strength WHERE scope=?", (scope,))
        store.db.execute("DELETE FROM strength_group WHERE scope=?", (scope,))
        events = store.events(scope)
        superseded = _superseded(store, scope)
        groups = []   # [gid, family, rep, days(set), last_at, event_ids]
        for event in events:
            text = event.get("text") or ""
            family = family_of(text) if event.get("role") == "user" else "other"
            if family == "other":
                _write(store, scope, event["event_id"], None, "other", HALF_LIFE_DAYS[0],
                       event.get("recorded_at") or "", now, superseded)
                continue
            target = None
            for group in groups:
                if group[1] == family and _similar(text, group[2]) >= 0.5:
                    target = group
                    break
            if target is None:
                target = [len(groups) + 1, family, text, set(), event.get("recorded_at") or "", []]
                groups.append(target)
            day = _day(event.get("recorded_at") or "")
            if day:
                target[3].add(day)
            if target[4] < (event.get("recorded_at") or ""):
                target[4] = event.get("recorded_at") or ""
            target[5].append(event["event_id"])
        for gid, family, rep, days, last_at, ids in groups:
            store.db.execute(
                "INSERT INTO strength_group(scope,gid,family,rep,days,last_at) VALUES(?,?,?,?,?,?)",
                (scope, gid, family, rep, len(days), last_at))
            for day in days:
                store.db.execute("INSERT OR IGNORE INTO strength_days(scope,gid,day) VALUES(?,?,?)",
                                 (scope, gid, day.isoformat()))
            half_life = HALF_LIFE_DAYS[min(max(len(days), 1), 3) - 1]
            for event_id in ids:
                _write(store, scope, event_id, gid, family, half_life, last_at, now, superseded)
        store.db.commit()
    return len(scopes)


def apply_events(store, scope, incoming, now=None):
    """ingest 时增量更新：新消息只影响它所在的那一组。"""
    _ensure(store)
    now = now or datetime.now().astimezone()
    superseded = _superseded(store, scope)
    for event in incoming:
        text = event.get('text') or ''
        family = family_of(text) if event.get('role') == 'user' else 'other'
        recorded = event.get('recorded_at') or ''
        if family == 'other':
            _write(store, scope, event['event_id'], None, 'other', HALF_LIFE_DAYS[0], recorded, now, superseded)
            continue
        rows = store.db.execute(
            'SELECT gid,rep,last_at FROM strength_group WHERE scope=? AND family=?',
            (scope, family)).fetchall()
        target = next((row for row in rows if _similar(text, row[1]) >= 0.5), None)
        if target is None:
            gid = store.db.execute(
                'SELECT COALESCE(MAX(gid),0)+1 FROM strength_group WHERE scope=?', (scope,)).fetchone()[0]
            store.db.execute(
                'INSERT INTO strength_group(scope,gid,family,rep,days,last_at) VALUES(?,?,?,?,0,?)',
                (scope, gid, family, text, recorded))
        else:
            gid = target[0]
        day = _day(recorded)
        if day:
            store.db.execute('INSERT OR IGNORE INTO strength_days(scope,gid,day) VALUES(?,?,?)',
                             (scope, gid, day.isoformat()))
        days = store.db.execute(
            'SELECT COUNT(*) FROM strength_days WHERE scope=? AND gid=?', (scope, gid)).fetchone()[0]
        last_at = max(target[2], recorded) if target is not None else recorded
        store.db.execute('UPDATE strength_group SET days=?, last_at=? WHERE scope=? AND gid=?',
                         (days, last_at, scope, gid))
        half_life = HALF_LIFE_DAYS[min(max(days, 1), 3) - 1]
        ids = [row[0] for row in store.db.execute(
            'SELECT event_id FROM memory_strength WHERE scope=? AND gid=?', (scope, gid))]
        ids.append(event['event_id'])
        for event_id in ids:
            _write(store, scope, event_id, gid, family, half_life, last_at, now, superseded)
    store.db.commit()


def strengths(store, scope):
    _ensure(store)
    rows = store.db.execute(
        "SELECT s.event_id, s.gid, s.importance, s.retention, s.strength, s.half_life,"
        " COALESCE(g.family,'other'), COALESCE(g.days,0), COALESCE(g.last_at,'')"
        " FROM memory_strength s LEFT JOIN strength_group g ON g.scope=s.scope AND g.gid=s.gid"
        " WHERE s.scope=? ORDER BY s.event_id", (scope,)).fetchall()
    out = []
    for event_id, gid, importance, retention, value, half_life, family, days, last_at in rows:
        pinned, muted = _override(store, scope, event_id)
        out.append({"event_id": event_id, "gid": gid, "family": family, "importance": importance,
                    "days": days, "retention": retention, "strength": value, "half_life": half_life,
                    "last_at": last_at, "pinned": pinned, "muted": muted})
    return out


def load_strengths(store, scope, event_ids):
    _ensure(store)
    if not event_ids:
        return {}
    marks = ",".join("?" for _ in event_ids)
    rows = store.db.execute(
        f"SELECT event_id, strength FROM memory_strength WHERE scope=? AND event_id IN ({marks})",
        (scope, *event_ids)).fetchall()
    return {row[0]: row[1] for row in rows}


def set_override(store, scope, event_id, pinned=None, muted=None, wrong=None):
    _ensure(store)
    pinned_now, muted_now = _override(store, scope, event_id)
    wrong_now = event_id in wrong_ids(store, scope)
    if pinned is not None:
        pinned_now = bool(pinned)
    if muted is not None:
        muted_now = bool(muted)
    if wrong is not None:
        wrong_now = bool(wrong)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.db.execute(
        "INSERT OR REPLACE INTO memory_overrides(scope,event_id,pinned,muted,wrong,updated_at)"
        " VALUES(?,?,?,?,?,?)", (scope, event_id, int(pinned_now), int(muted_now), int(wrong_now), stamp))
    store.db.commit()
    return {"event_id": event_id, "pinned": pinned_now, "muted": muted_now, "wrong": wrong_now}


def clarity(value, last_at=None, pinned=False, now=None):
    """「TA的记忆」页上的清楚程度，用的就是检索真正在用的强度（不另造一个好看的分数）。

    一直记着 / 清楚 / 有点模糊 / 快忘了。「快忘了」和检索里的「记不太清」同一个门槛（强度与一个月）。
    """
    if pinned:
        return "一直记着"
    if value is None:
        return "清楚"
    if value < FUZZY_BELOW:
        try:
            moment = datetime.fromisoformat(last_at.replace("Z", "+00:00")) if last_at else None
        except ValueError:
            moment = None
        now = now or datetime.now(timezone.utc)
        if moment is None or (now - moment).days >= FUZZY_MIN_AGE_DAYS:
            return "快忘了"
        return "有点模糊"
    if value < 0.45:
        return "有点模糊"
    return "清楚"


def wrong_ids(store, scope):
    """用户在「TA的记忆」里标了「这条记错了」的 event_id。"""
    _ensure(store)
    return {row[0] for row in store.db.execute(
        "SELECT event_id FROM memory_overrides WHERE scope=? AND wrong=1", (scope,))}


# 用户亲口说「别记了 / 忘掉吧 / 当我没说」：原文仍在档案里（逐字可查是底线，可撤销），
# 但检索和所有主动出现的地方都当它不存在。「你不会忘了吧」这种是反问，不算。
FORGET_REQUEST = __import__("re").compile(
    r"别记(?:了|住|着|这个|那个|下来)|不要记(?:了|住|着|下来)|不用记(?:了|住|着)"
    r"|忘掉(?:吧|它|这个|那个|算了)|把(?:它|这个|那个|这句|那句)?忘了吧?|当我没说")
_FORGET_FILLER = __import__("re").compile(r"^(?:算了|刚才|刚刚|那句|这句|那个|这个|上一句|前面那句|哈+|嗯+|唉|啊|吧|的|话)*$")


def _forget_standalone(text):
    """整句只是请求（「刚才那句别记了」）：要忘的是上一句。"""
    import re
    for clause in re.split(r"[，,。；;！!？?~～\s]+", text):
        if not clause or FORGET_REQUEST.search(clause):
            continue
        if not _FORGET_FILLER.match(clause):
            return False
    return True


def forgotten_ids(store, scope):
    """用户要求忘掉的原话 event_id 集合（只读派生，不改档案）：说过「别记了」的，加上标了「记错了」的。"""
    return said_forget_ids(store, scope) | wrong_ids(store, scope)


def said_forget_ids(store, scope):
    """用户在聊天里说「别记了 / 忘掉吧」要忘掉的原话。"""
    import json
    rows = store.db.execute(
        "SELECT seq, payload FROM events WHERE scope=? AND (payload LIKE '%别记%' OR payload LIKE '%不要记%'"
        " OR payload LIKE '%不用记%' OR payload LIKE '%忘%' OR payload LIKE '%当我没说%') ORDER BY seq",
        (scope,)).fetchall()
    hidden = set()
    for seq, payload in rows:
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        text = event.get('text') or ''
        if event.get('role') != 'user' or not FORGET_REQUEST.search(text):
            continue
        hidden.add(event['event_id'])
        if _forget_standalone(text):
            for (earlier,) in store.db.execute(
                    "SELECT payload FROM events WHERE scope=? AND seq<? ORDER BY seq DESC LIMIT 6", (scope, seq)):
                previous = json.loads(earlier)
                if previous.get('role') == 'user':
                    hidden.add(previous['event_id'])
                    break
    return hidden


FUZZY_BELOW = 0.15      # 检索里加「记不太清」：强度低于此值……
FUZZY_MIN_AGE_DAYS = 30  # ……而且至少是一个月前的事（两周前的事标记不清不像人，模型也会不敢答）


SENSITIVE = ('diet', 'emotion')   # 忌口/过敏、心情：永不主动出现
PROACTIVE_FLOOR = 0.2


def proactive_allowed(store, scope, event_id):
    """主动出现的地方（开场快照、便利贴、到期提醒）能不能提这一条。"""
    _ensure(store)
    if event_id in forgotten_ids(store, scope):
        return False
    row = store.db.execute(
        "SELECT s.strength, COALESCE(g.family,'other') FROM memory_strength s"
        " LEFT JOIN strength_group g ON g.scope=s.scope AND g.gid=s.gid"
        " WHERE s.scope=? AND s.event_id=?", (scope, event_id)).fetchone()
    if not row:
        return True   # 老档案还没建强度表：保持原来的行为
    _pinned, muted = _override(store, scope, event_id)
    if muted:
        return False
    value, family = row
    if family in SENSITIVE:
        return False
    return value >= PROACTIVE_FLOOR


def move_for_strength(value):
    """强度 ≥0.6 不挪；0.3–0.6 挪 1–2 名；<0.3 挪 3–4 名；上限 4。"""
    if value is None or value >= 0.6:
        return 0
    if value >= 0.45:
        return 1
    if value >= 0.3:
        return 2
    if value >= 0.15:
        return 3
    return 4
