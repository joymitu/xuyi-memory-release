"""记忆看板的数据：以"你"为中心的导图 + 四周的便利贴。

导图上每一项都用与回忆相同的重点规则求出（逐项用代表性问句问一遍 current_highlight），
所以看板显示的正是小雅被问到时会想起的内容；改口前的说法来自被更替的原话。
便利贴是会变的东西：刚刚想起、等你确认（疑似改口）、还没下文（开放环）、近期约定。
每条便利贴都同时给出"事实"（中性的一句话，交给宿主模型按用户实际人设改写）和
"模板"（零模型调用时直接展示的现成文案），适配器不在时看板照样能看。
敏感类（心情）只出现在导图里且标注折叠。
"""
import json
import re
from datetime import date, datetime

from .consolidate import family_events, upcoming
from .highlights import FAMILIES, current_highlight

PEER_PREFIX = "qq:c2c:"
PET_WORDS = {"猫": "猫", "狗": "狗", "兔子": "兔子", "仓鼠": "仓鼠"}


def _quote(event, limit=40):
    text = " ".join(event["text"].split())
    return text if len(text) <= limit else text[:limit] + "…"


def _day(event):
    return event["recorded_at"][:10]


def _item(label, highlight, events_by_id, sensitive=False):
    record, sources, replaced = highlight
    before = sorted((events_by_id[i] for i in replaced if i in events_by_id), key=lambda e: e["recorded_at"])
    return {"label": label, "value": record["值"], "quote": _quote(sources[0]), "date": _day(sources[0]),
            "event_id": sources[0]["event_id"],
            "history": [{"quote": _quote(e), "date": _day(e), "event_id": e["event_id"]} for e in before],
            "sensitive": sensitive}


def _ask(scope, query, events):
    return current_highlight(scope, query, events)


def board(store, scope, today=None, recalls=8):
    """Everything the board page draws for one QQ contact; read-only."""
    today = today or date.today()
    peer = scope.removeprefix(PEER_PREFIX)
    events = family_events(store, scope)
    events_by_id = {e["event_id"]: e for e in events}
    superseded = {e["supersedes"] for e in events if "supersedes" in e}
    branches = []

    def branch(name, items):
        items = [item for item in items if item]
        if items:
            branches.append({"name": name, "items": items})

    name_family, pref_family, appt_family, loop_family, emotion_family, diet_family = FAMILIES
    keys = []
    for key, _event, _match in name_family._statements(peer, events, superseded):
        if key not in keys:
            keys.append(key)
    nickname = _ask(scope, "以后怎么称呼我？", events)
    pets = []
    for key in keys:
        if key in PET_WORDS:
            found = _ask(scope, f"我家{key}叫什么？", events)
            if found:
                pets.append(_item(f"{key}的名字", found, events_by_id))
    branch("宠物", pets)

    parsed = []
    from .highlights import _archived_user_lines, _by_event_time
    for event, text in _archived_user_lines(events, superseded):
        if event["sender_id"] == peer and (item := pref_family._parse(text)):
            parsed.append((event, *item))
    parsed.sort(key=_by_event_time)
    taste, other = [], []
    for key in pref_family._history(parsed):
        if isinstance(key, tuple):
            continue
        noun = next((n for n in pref_family.NOUNS if pref_family.CLASS_OF.get(n, n) == key), key)
        found = _ask(scope, f"我最喜欢的{noun}是什么？", events)
        if found:
            (taste if key in ("饮品", "食物", "零食", "水果") else other).append(
                _item(found[0]["属性"].removeprefix("最喜欢的"), found, events_by_id))
    diet = _ask(scope, "我有什么忌口过敏？", events)
    # 忌口过敏和心情一样算敏感：TA永远不主动提，页面上也先模糊、只在你提到时才说。
    branch("吃喝", taste + ([_item("忌口过敏", diet, events_by_id, sensitive=True)] if diet else []))
    branch("喜好", other)

    friends = []
    slots, ambiguous = appt_family.live_states(peer, events, superseded)
    if not ambiguous:
        for slot in slots:
            status = {"planned": slot["time"], "cancelled": f"{slot['time']}，已取消",
                      "moved": f"{slot['time']}改到{slot.get('new')}"}[slot["status"]]
            source = slot["last"]
            friends.append({"label": slot["who"], "value": status, "quote": _quote(source), "date": _day(source),
                            "event_id": source["event_id"],
                            "history": [] if source is slot["make"] else
                            [{"quote": _quote(slot["make"]), "date": _day(slot["make"]),
                              "event_id": slot["make"]["event_id"]}], "sensitive": False})
    branch("朋友", friends)

    loops = loop_family.open_loops(peer, events, superseded)
    branch("计划", [{"label": "想做的事", "value": loop["值"], "quote": _quote(loop["来源"]),
                    "date": _day(loop["来源"]), "event_id": loop["来源"]["event_id"], "history": [],
                    "sensitive": False} for loop in loops[-6:]])
    mood = _ask(scope, "我最近心情怎么样？", events)
    branch("心情", [_item("最近的心情", mood, events_by_id, sensitive=True)] if mood else [])

    seeds = _note_seeds(store, scope, peer, events, superseded, today, recalls, loops)
    from .strength import proactive_allowed
    seeds = [seed for seed in seeds if not seed.get("event_id")
             or proactive_allowed(store, scope, seed["event_id"])]
    all_events = store.db.execute(
        "SELECT COUNT(*), MIN(json_extract(payload,'$.recorded_at')) FROM events WHERE scope=?",
        (scope,)).fetchone()
    first = (all_events[1] or "")[:10]
    known = (today - date.fromisoformat(first)).days + 1 if first else 0
    from .strength import load_strengths, strengths as _strength_rows

    def _annotate(items, key="event_id"):
        by_id = {row["event_id"]: row for row in _strength_rows(store, scope)}
        out = []
        for item in items:
            row = by_id.get(item.get(key))
            if row:
                item = dict(item, strength=round(row["strength"], 3),
                            pinned=row["pinned"], muted=row["muted"])
            out.append(item)
        return out

    memory, forgotten = _memory_page(store, scope, branches, nickname, events_by_id)
    return {"scope": scope, "center": {"name": nickname[0]["值"] if nickname else "你",
                                       "since": first, "days": known, "messages": all_events[0]},
            "branches": branches,
            "memory": memory, "forgotten": forgotten,
            "notes": _annotate(_template_notes(seeds)),
            "note_facts": _annotate([{"kind": seed["kind"], "fact": seed["fact"],
                            **({"event_id": seed["event_id"]} if seed.get("event_id") else {}),
                            **({"at": seed["at"]} if seed.get("at") else {})} for seed in seeds])}


def _memory_page(store, scope, branches, nickname, events_by_id):
    """「TA的记忆」页：按类别列出TA记得的你的事，每条带清楚程度和开关状态。

    用户聊天里说过「别记了」的整条不出现（他本来就不想看到）；在页面上标了「记错了」的
    放进 forgotten，可以恢复。
    """
    from .strength import clarity, said_forget_ids, strengths as _rows, wrong_ids
    said = said_forget_ids(store, scope)
    wrong = wrong_ids(store, scope)
    by_id = {row["event_id"]: row for row in _rows(store, scope)}
    groups = []
    if nickname:
        groups.append({"name": "称呼", "items": [_item("你让TA这么叫你", nickname, events_by_id)]})
    # 「朋友：小周 / 周日」读着别扭——这一类其实是约定，写成「约定：和小周 / 周日」。
    groups += [{"name": "约定", "items": [dict(i, label=f"和{i['label']}") for i in b["items"]]}
               if b["name"] == "朋友" else {"name": b["name"], "items": list(b["items"])} for b in branches]
    memory, forgotten = [], []
    for group in groups:
        kept = []
        for item in group["items"]:
            ids = {item.get("event_id")} | {h.get("event_id") for h in item.get("history", [])}
            if ids & said:
                continue
            row = by_id.get(item.get("event_id")) or {}
            item = dict(item, pinned=bool(row.get("pinned")), muted=bool(row.get("muted")),
                        clarity=clarity(row.get("strength"), row.get("last_at"), bool(row.get("pinned"))))
            if item.get("event_id") in wrong:
                forgotten.append(dict(item, group=group["name"]))
            else:
                kept.append(item)
        if kept:
            memory.append({"name": group["name"], "items": kept})
    return memory, forgotten


def _note_seeds(store, scope, peer, events, superseded, today, limit, loops):
    """What the sticky notes are about: a neutral fact plus 小雅's own template wording.

    The fact is what the host model gets (it rewrites it in the user's own persona);
    the template is what the page shows when no host is available or the model fails.
    """
    notes = []
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='access_log'").fetchone()
    rows = store.db.execute("SELECT query,event_ids,recorded_at FROM access_log WHERE scope=? "
                            "ORDER BY seq DESC LIMIT ?", (scope, limit * 3)).fetchall() if exists else []
    seen = set()
    for query, ids, at in rows:
        # 只展示"你自己问的话"触发的回忆：模型调用记忆工具时用的查询词（如"猫 年糕"）不是你说的，
        # 展示出来既不对口吻，还可能是旧说法。自动回忆的查询词恰好是你那条消息的原文。
        said = store.db.execute(
            "SELECT 1 FROM events WHERE scope=? AND json_extract(payload,'$.role')='user' "
            "AND json_extract(payload,'$.text')=? LIMIT 1", (scope, query)).fetchone()
        if not said:
            continue
        candidates = [store.get(scope, i) for i in json.loads(ids)]
        recalled = next((e for e in candidates if e and e["text"].strip() != query.strip()), None)
        if not recalled or query in seen:
            continue
        seen.add(query)
        found = current_highlight(scope, query, events)
        gist = found[0]["值"] if found else _quote(recalled, 16)
        asked = _short(query.rstrip("？?。！!"), 12)
        notes.append({"kind": "recall", "at": at[:16].replace("T", " "),
                      "fact": f"他刚问你「{asked}」，你想起来：{gist}",
                      "text": f"你问我「{asked}」——想起来啦：{gist}～", "quote": _quote(recalled),
                      "event_id": recalled["event_id"]})
        if len([n for n in notes if n["kind"] == "recall"]) >= 3:
            break
    for family in (FAMILIES[0], FAMILIES[1]):
        for pair in family.suspected_pairs(peer, events, superseded):
            notes.append({"kind": "confirm",
                          "fact": f"他先说「{pair['旧值']}」，后来说「{pair['新值']}」，到底哪个还没确认",
                          "text": f"你说过「{pair['旧值']}」，后来又说「{pair['新值']}」……到底是哪个呀？",
                          "event_id": pair["新来源"]})
    for loop in loops[-3:]:
        notes.append({"kind": "followup", "fact": f"他提过想{loop['值']}，还没下文",
                      "text": f"你说想{loop['值']}，后来怎么样啦？找机会问问你～",
                      "event_id": loop["来源"]["event_id"]})
    for row in upcoming(store, scope, today=today, events=events)[:2]:
        if row["date"]:
            when = "今天" if row["date"] == today.isoformat() else row["时间"]
            notes.append({"kind": "plan", "fact": f"{when}他和{row['who']}有约",
                          "text": f"{when}和{row['who']}有约，记得提醒你一下。",
                          "event_id": row["event_id"]})
    # 今天聊过的事也要贴上（用户要的）：不管有没有重点事实，都取今天最像"一件事"的一两句。
    from .retrieval import local_moment
    todays = []
    for event in store.events(scope):
        if event.get("role") != "user" or event.get("sender_id") != peer:
            continue
        try:
            if local_moment(event["recorded_at"]).date() != today:
                continue
        except (KeyError, ValueError):
            continue
        said = topic_text(event.get("text"))
        if said:
            todays.append((event, said))
    return (pick_topics(todays) + notes)[:8]


ASK_TO_HER = re.compile(r"什么|啥|哪|谁|怎么|多少|几[个只天号点次]|记得")


def topic_text(text):
    """这句用户原话能不能当便利贴的「话题」：能就返回去掉宿主外壳后的原话，不能返回 None。

    寒暄、语气词、宿主注入的上下文不算；**问句也不算**——「你还记得我说的那家店叫啥不」
    是他问她的，不是他告诉她的事（2026-11-11 用户反馈）。
    """
    from .retrieval import is_bare_question
    said = (text or "").strip()
    if "[Current message]" in said:
        said = said.split("[Current message]", 1)[1].strip()
    if said.startswith("[") or "Current runtime context" in said or "system-reminder" in said:
        return None
    if len(said) < 6 or said.startswith(("在吗", "哈哈", "嗯", "哦")):
        return None
    if said.rstrip().endswith(("？", "?")) or is_bare_question({"role": "user", "text": said}):
        return None
    # 问她的话：有「你」又有疑问词（打错字的「……来自」也能认出来）。
    if "你" in said and ASK_TO_HER.search(said):
        return None
    return said


def pick_topics(candidates, limit=2, when="今天"):
    """从一天的 (event, 原话) 里挑最像「一件事」的几句，做成话题便利贴（长的优先、开头相同的只留一句）。"""
    topics, picked = [], []
    for event, said in sorted(candidates, key=lambda pair: -len(pair[1])):
        if any(said[:8] == other[:8] for other in picked):
            continue
        picked.append(said)
        topics.append({"kind": "topic",
                       "at": (event.get("recorded_at") or "")[:16].replace("T", " "),
                       "fact": f"他{when}提到：{said}",
                       "text": f"你说「{_short(said, 18)}」，我记着呢。",
                       "event_id": event["event_id"]})
        if len(picked) >= limit:
            break
    return topics


NOTE_FIELDS = ("kind", "at", "text", "quote", "event_id")


def _template_notes(seeds):
    """The template wording the page falls back to; no model involved."""
    return [{key: seed[key] for key in NOTE_FIELDS if key in seed} for seed in seeds]


def _short(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def contacts(store):
    """QQ private-chat scopes, newest activity first, with a friendly label."""
    out = []
    for (scope,) in store.db.execute("SELECT DISTINCT scope FROM events WHERE scope LIKE 'qq:c2c:%'"):
        row = store.db.execute("SELECT COUNT(*), MAX(json_extract(payload,'$.recorded_at')) FROM events "
                               "WHERE scope=?", (scope,)).fetchone()
        nickname = current_highlight(scope, "以后怎么称呼我？", family_events(store, scope))
        out.append({"scope": scope, "label": nickname[0]["值"] if nickname else None,
                    "messages": row[0], "last": (row[1] or "")[:10]})
    out.sort(key=lambda c: c["last"], reverse=True)
    for n, contact in enumerate(out, 1):
        contact["label"] = contact["label"] or f"好友 {n}"
    return out
