"""日记本：按天翻的聊天原话，页边贴当天的便利贴。

只读、零模型调用。正文只排"你说过的话"（原话，不改写、不总结），
她的批注来自看板那套便利贴（模板或宿主模型写的），按事发那天贴到页边。
时间按本地时区分组：晚上 23:50 的话算今天，不会因为 UTC 跑到明天去。
"""
import re
from datetime import date, datetime, timezone

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
NOTE_KINDS = ("kind", "text", "at", "event_id", "quote")
# 目录里只留一句有内容的话，寒暄和语气词不值得占一行。
FILLER = re.compile(r"^(?:在吗|在不在|在不|你好|您好|嗨+|哈+|呵+|嗯+|哦+|喔+|好|好的|行|早|早安|晚安|"
                    r"拜拜|再见|睡了|吃了吗|干什么呢)[。！？!?～~，,.\s]*$")


def day_label(entries, limit=10):
    """一句能当目录的短话：优先他说的第一句有内容的，实在没有就退回第一句。"""
    for entry in entries:
        text = " ".join(entry["text"].split()).strip()
        if len(text) >= 4 and not FILLER.match(text):
            return text if len(text) <= limit else text[:limit] + "…"
    if entries:
        text = " ".join(entries[0]["text"].split()).strip()
        return text if len(text) <= limit else text[:limit] + "…"
    return ""


def _local(iso):
    moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if moment.tzinfo is None:      # access_log 里的时间不带时区，按 UTC 存
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone()


def local_day(iso):
    return _local(iso).date().isoformat()


def local_clock(iso):
    return _local(iso).strftime("%H:%M")


def _note_day(note, by_event):
    # "刚想起"贴在"他问的那天"，其余贴在这条记忆来源的那天。
    if note.get("kind") == "recall" and note.get("at"):
        return _local(note["at"]).date().isoformat()
    day = by_event.get(note.get("event_id") or "")
    if day:
        return day
    if note.get("at"):
        return _local(note["at"]).date().isoformat()
    return None


def day_messages(store, scope, day, limit=120, rows=4000):
    """Everything said that day (both sides), oldest first — the raw material for a diary entry.

    聊得再多也只取 `limit` 条：不是截掉开头只留结尾，而是从头到尾均匀抽，
    保证早上和晚上都还在，模型看到的是"这一天的样子"而不是"最后一百句"。
    """
    data = store.db.execute(
        "SELECT json_extract(payload,'$.recorded_at'), json_extract(payload,'$.role'), "
        "json_extract(payload,'$.text') FROM events WHERE scope=? "
        "ORDER BY json_extract(payload,'$.recorded_at') DESC LIMIT ?",
        (scope, rows)).fetchall()
    out = []
    for at, role, text in data:
        if not at or not text or local_day(at) != day:
            continue
        out.append({"time": local_clock(at), "role": role, "text": text})
    out.sort(key=lambda message: message["time"])
    if len(out) <= limit:
        return out
    step = (len(out) - 1) / (limit - 1)
    picked, seen = [], set()
    for i in range(limit):
        index = round(i * step)
        if index not in seen:
            seen.add(index)
            picked.append(out[index])
    return picked


def diary(store, scope, notes=None, days=30, per_day=60, limit=4000):
    """Last `days` local days of what the other person said, with that day's sticky notes."""
    rows = store.db.execute(
        "SELECT json_extract(payload,'$.event_id'), json_extract(payload,'$.recorded_at'), "
        "json_extract(payload,'$.role'), json_extract(payload,'$.text') FROM events "
        "WHERE scope=? ORDER BY json_extract(payload,'$.recorded_at') DESC LIMIT ?",
        (scope, limit)).fetchall()
    from .board import pick_topics, topic_text
    buckets, order, by_event, topics_by_day = {}, [], {}, {}
    for event_id, at, role, text in rows:
        if not at:
            continue
        day = local_day(at)
        by_event[event_id] = day
        if day not in buckets:
            buckets[day] = {"date": day, "weekday": WEEKDAYS[date.fromisoformat(day).weekday()],
                            "count": 0, "entries": [], "notes": []}
            order.append(day)
        bucket = buckets[day]
        bucket["count"] += 1
        if role == "user" and text and len(bucket["entries"]) < per_day:
            bucket["entries"].append({"time": local_clock(at), "text": text})
        said = topic_text(text) if role == "user" else None
        if said:
            topics_by_day.setdefault(day, []).append(({"event_id": event_id, "recorded_at": at}, said))
    for bucket in buckets.values():
        bucket["entries"].reverse()  # rows came newest-first
    for note in notes or []:
        day = _note_day(note, by_event)
        if day in buckets:
            buckets[day]["notes"].append({key: note[key] for key in NOTE_KINDS if key in note})
    # 过了零点，那天聊过的事还留在那一页（以前话题便利贴只算「今天」，第二天就没了）。
    # 已经有话题便利贴的那天（今天的，模型写过的）不重复贴；以前的日子用模板句，不再交给模型。
    for day in order[:days]:
        bucket = buckets[day]
        if not any(note.get("kind") == "topic" for note in bucket["notes"]):
            for topic in pick_topics(topics_by_day.get(day, []), when="那天"):
                bucket["notes"].append({key: topic[key] for key in NOTE_KINDS if key in topic})
    return {"scope": scope, "days": [dict(buckets[day], label=day_label(buckets[day]["entries"]))
                                     for day in order[:days]], "all_days": len(order)}
