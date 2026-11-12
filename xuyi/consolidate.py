"""Offline consolidation pass: re-derive indexes, audit skipped statements and
probe whether rule-covered memories are actually reachable through the read
path. Everything here is a deterministic re-derivation of the verbatim
archive — no model calls, no rewrites; running it twice yields the same
report (the sleep-replay principle, kept cheap and idempotent).
"""
import re
from datetime import datetime, timedelta

from . import index as xuyi_index
from .audit import audit
from .highlights import FAMILIES, current_highlight
from .retrieval import HEADER, canonical, render_line
from .embeddings import ByteCounter

# One probe per family branch: the name family gates pet names and nicknames
# with an XOR, so neither query can reach the other branch's statements.
PROBES = ((0, "我的猫叫什么？"), (0, "以后怎么称呼我？"),
          (1, "我现在最爱喝什么？"), (2, "和小林的安排还要赴约吗？"),
          (3, "最近有什么打算？"))
STRENGTH = {"存而不达": 0, "已被更替": 1, "当前候选": 2}
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
DAY_OFFSET = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}


def _resolve_date(phrase, base):
    """Resolve a narrow set of time phrases against a base date, or None.

    Recurring phrases (周X) resolve to the NEXT occurrence from the base —
    "周六见小林" stays alive every week until cancelled, so the base is
    today for due windows, not the day the promise was made.
    """
    if phrase in DAY_OFFSET:
        return base + timedelta(days=DAY_OFFSET[phrase])
    if phrase.startswith(("周", "星期")) and len(phrase) <= 3 and phrase[-1] in WEEKDAY:
        days = (WEEKDAY[phrase[-1]] - base.weekday()) % 7 or 7
        return base + timedelta(days=days)
    return None


def family_events(store, scope):
    """Events the highlight families can read: the highlight index when fresh, else everything."""
    from . import index
    indexed = index.highlight_events(store, scope)
    return indexed if indexed is not None else store.events(scope)


def tail_events(store, scope, needed):
    """Shortest archive tail for which needed(tail, complete) gives an answer; same result as all events."""
    import json
    limit = 256
    while True:
        rows = store.db.execute("SELECT payload FROM events WHERE scope=? ORDER BY seq DESC LIMIT ?",
                                (scope, limit)).fetchall()
        tail = [json.loads(row[0]) for row in reversed(rows)]
        complete = len(rows) < limit
        answer = needed(tail, complete)
        if answer is not None or complete:
            return answer
        limit *= 4


def upcoming(store, scope, today=None, events=None):
    """Live appointments with dates resolved from today, sorted; None dates last."""
    events = family_events(store, scope) if events is None else events
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    peer = scope.removeprefix("qq:c2c:") if scope.startswith("qq:c2c:") else None
    if not peer:
        return []
    today = today or datetime.now().date()
    family = FAMILIES[2]
    slots, ambiguous = family.live_states(peer, events, superseded)
    if ambiguous:
        return []
    rows = []
    for slot in slots:
        if slot["status"] not in ("planned", "moved"):
            continue
        phrase = slot.get("new") if slot["status"] == "moved" else slot["time"]
        date = _resolve_date(phrase, today)
        rows.append({"who": slot["who"], "时间": phrase,
                     "date": date.isoformat() if date else None,
                     "event_id": slot["last"]["event_id"]})
    return sorted(rows, key=lambda row: (row["date"] is None, row["date"] or "", row["who"]))


DUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS due_sent(
    scope TEXT NOT NULL,event_id TEXT NOT NULL,date TEXT NOT NULL,
    PRIMARY KEY(scope,event_id,date));
"""

# The one naturalness line: proactive mention is welcome once, then silence.
FOLLOWUP_GUIDE = ("以上是想起的旧事。语境合适时可像随口想起那样，主动问起其中一件将到期或没下文的事，一轮至多一次；对方没接话就不要再提。"
                  "若「多久前」是刚刚或几分钟、几小时前，就当是接着刚才的话聊，不要说好久不见。")


def _due_appointments(store, scope, today, events=None):
    """Appointments whose resolved date is today or tomorrow, unmarked yet."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='due_sent'").fetchone():
        store.db.executescript(DUE_SCHEMA)
    rows = [row for row in upcoming(store, scope, today=today, events=events)
            if row["date"] in (today.isoformat(), (today + timedelta(days=1)).isoformat())]
    if not rows:
        return []
    fresh = []
    for row in rows:
        sent = store.db.execute(
            "SELECT 1 FROM due_sent WHERE scope=? AND event_id=? AND date=?",
            (scope, row["event_id"], row["date"])).fetchone()
        if not sent:
            fresh.append(row)
    return fresh


def _session_of(event):
    return event["event_id"].split(":")[1] if event["event_id"].startswith("dsh:") else None


def _last_thread_in_tail(tail, complete, current=None):
    """None = read further back; False = decided, no thread. Equal to _last_thread on all events:
    once the tail holds any user line of the previous session it also holds the latest one."""
    if complete or not tail:
        return _last_thread(None, None, tail, current=current) or False
    guessed = current is None
    if guessed:
        current = _session_of(tail[-1])
        if current is None:
            return False
    previous = next((_session_of(e) for e in reversed(tail[:-1] if guessed else tail)
                     if _session_of(e) and _session_of(e) != current), None)
    if previous is None or not any(_session_of(e) == previous and e["role"] == "user" for e in tail):
        return None
    return _last_thread(None, None, tail, current=None if guessed else current) or False


def _last_thread(store, scope, events, max_chars=40, current=None):
    """The previous session's last user message — the thread to resume.

    Sessions are distinct dsh:<sessionId>:... prefixes in event order; the
    natural opener after an automatic rotation is picking the thread back
    up ("昨天说到哪儿了"), not reintroducing oneself.

    ``current`` is the session the host says is running now. Without it the
    current session is inferred from the archive's last event — which is wrong
    at a session's first turn, because the host prepares the request before it
    emits the user message, so that message is not archived yet.
    """
    def session_of(event):
        return event["event_id"].split(":")[1] if event["event_id"].startswith("dsh:") else None
    if current is None:
        if len(events) < 2:
            return None
        current = session_of(events[-1])
        if current is None:
            return None
        scan = events[:-1]
    else:
        scan = events
    previous = None
    for event in reversed(scan):
        sid = session_of(event)
        if sid and sid != current:
            previous = sid
            break
    if previous is None:
        return None
    last_user = next((event for event in reversed(events)
                      if session_of(event) == previous and event["role"] == "user"), None)
    if not last_user or len(last_user["text"]) > 120:
        return None
    text = last_user["text"].strip()
    # Bare questions and check-ins ("在吗"/"最近怎么样") carry no thread.
    if len(text) <= 12 and re.fullmatch(
            r"(?:在吗|最近怎么样|怎么样|还好吗|睡了吗|忙吗|吃了吗|哈喽|嗨|hello|hi)[？?！!。]?",
            text, re.I):
        return None
    if text.endswith("？") and len(text) <= 12:
        return None
    summary = text[:max_chars] + ("…" if len(text) > max_chars else "")
    # The last user line alone is often just a reaction ("又在瞎编了"); the last few lines of both
    # sides show what they were actually talking about and what she herself said.
    recent = [event for event in events if session_of(event) == previous
              and event["role"] in ("user", "assistant") and event["text"].strip()][-4:]
    exchange = [("他：" if event["role"] == "user" else "我：")
                + " ".join(event["text"].split())[:18] + ("…" if len(" ".join(event["text"].split())) > 18 else "")
                for event in recent]
    return {"摘要": summary, "来源": last_user, "最后几句": exchange, "结束于": recent[-1]["recorded_at"],
            "_recent_ids": [event["event_id"] for event in recent]}


def snapshot(store, scope, budget=800, counter=None, today=None, current_session=None):
    """Compact session-start state: due appointments, family highlights, open loops.

    Due lines (today/tomorrow appointments, once per appointment per date)
    come first — the validated proactive hook; family highlights follow with
    verbatim sources; up to two open intention loops close it out. A one-line
    guide bounds proactive mention to once per turn. Zero-injection when
    nothing qualifies.
    """
    from .retrieval import chunks
    counter = counter or ByteCounter()
    # Long archives: families read the highlight index, the resume hook reads the tail.
    events = family_events(store, scope)
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    lines = []
    sources = []
    due_rows = []
    emitted_due = []
    emitted_ids = set()
    used = counter.count(HEADER)
    peer = scope.removeprefix("qq:c2c:") if scope.startswith("qq:c2c:") else None
    explicit_today = today   # None = real clock, so "多久前" can be minute-level
    today = today or datetime.now().date()
    resume = tail_events(store, scope,
                         lambda tail, complete: _last_thread_in_tail(tail, complete, current_session)) or None
    from .strength import proactive_allowed
    if resume and not proactive_allowed(store, scope, resume["来源"]["event_id"]):
        resume = None
    if resume:
        # 「最后几句」里也不能带出用户要求忘掉的话。
        from .strength import forgotten_ids
        forgotten = forgotten_ids(store, scope)
        if forgotten and any(event_id in forgotten for event_id in resume.get("_recent_ids", ())):
            resume = None
    if resume:
        from .retrieval import relative_day
        record = {"上次聊到": resume["摘要"], "最后几句": resume["最后几句"],
                  "多久前": relative_day(resume["结束于"], today=explicit_today)}
        record_line = canonical({"unverified_resume": record})
        source_line = render_line(resume["来源"])
        cost = counter.count(record_line) + counter.count(source_line) + 2
        if used + cost <= budget and resume["来源"]["event_id"] not in emitted_ids:
            used += cost
            lines.append(record_line)
            lines.append(source_line)
            sources.append(dict(resume["来源"], unverified_resume=record))
            emitted_ids.add(resume["来源"]["event_id"])
    if peer:
        due_rows = _due_appointments(store, scope, today, events)
    for row in due_rows:
        record = {"约定": f"和{row['who']}有约", "时间": f"{row['时间']}（{'今天' if row['date'] == today.isoformat() else '明天'}）"}
        record_line = canonical({"unverified_due": record})
        piece = chunks([next(event for event in events if event["event_id"] == row["event_id"])])[0]
        from .strength import proactive_allowed
        if not proactive_allowed(store, scope, piece["event_id"]):
            continue
        source_line = render_line(piece)
        cost = counter.count(record_line) + counter.count(source_line) + 2 \
            + (1 if lines else 0)
        if used + cost > budget or piece["event_id"] in emitted_ids:
            continue
        used += cost
        lines.append(record_line)
        lines.append(source_line)
        sources.append(dict(piece, unverified_due=record))
        emitted_due.append(row)
        emitted_ids.add(piece["event_id"])
    for family_index, query in PROBES:
        highlight = current_highlight(scope, query, events)
        if not highlight:
            continue
        record, source_events, _replaced = highlight
        piece = chunks([source_events[0]])[0]
        from .strength import proactive_allowed
        if not proactive_allowed(store, scope, piece["event_id"]):
            continue
        record_line = canonical({"unverified_current_highlight": record})
        source_line = render_line(piece)
        cost = counter.count(record_line) + counter.count(source_line) + 2 \
            + (1 if lines else 0)
        if used + cost > budget or piece["event_id"] in emitted_ids:
            continue
        used += cost
        lines.append(record_line)
        lines.append(source_line)
        sources.append(dict(piece, unverified_current_highlight=record))
        emitted_ids.add(piece["event_id"])
    if peer:
        loops = FAMILIES[3].open_loops(peer, events, superseded)[:2]
        for loop in loops:
            piece = chunks([loop["来源"]])[0]
            from .strength import proactive_allowed
            if not proactive_allowed(store, scope, piece["event_id"]):
                continue
            record_line = canonical({"unverified_open_loop": {"想做的事": loop["值"]}})
            source_line = render_line(piece)
            cost = counter.count(record_line) + counter.count(source_line) + 2 \
                + (1 if lines else 0)
            if used + cost > budget or piece["event_id"] in emitted_ids:
                break
            used += cost
            lines.append(record_line)
            lines.append(source_line)
            sources.append(dict(piece, unverified_open_loop={"想做的事": loop["值"]}))
            emitted_ids.add(piece["event_id"])
    if lines and used + counter.count(FOLLOWUP_GUIDE) + 1 <= budget:
        lines.append(FOLLOWUP_GUIDE)
    if emitted_due:
        with store.db:
            store.db.executemany(
                "INSERT OR IGNORE INTO due_sent(scope,event_id,date) VALUES(?,?,?)",
                [(scope, row["event_id"], row["date"]) for row in emitted_due])
    context = HEADER + "\n".join(lines) if lines else ""
    return {"scope": scope, "context": context, "sources": sources,
            "budget": budget, "used": counter.count(context), "unit": counter.unit,
            "due": due_rows}


def profile(store, scope, budget=1600, counter=None, today=None):
    """A "what do you remember about me" answer: first-contact date, current
    highlights, due/open items — the structured counterpart of fuzzy recall."""
    counter = counter or ByteCounter()
    events = store.events(scope)
    if not events:
        return {"context": "", "sources": [], "budget": budget, "used": 0, "unit": counter.unit}
    lines = []
    sources = []
    used = counter.count(HEADER)
    first = min(event["recorded_at"] for event in events)[:10]
    intro = canonical({"unverified_profile": {"初识": first, "事件数": len(events)}})
    if used + counter.count(intro) + 1 <= budget:
        used += counter.count(intro) + 1
        lines.append(intro)
    snap = snapshot(store, scope, budget=budget - used, counter=counter, today=today)
    if snap["context"]:
        body = snap["context"].split("\n", 1)[1]
        for line in body.split("\n"):
            cost = counter.count(line) + 1
            if used + cost > budget:
                break
            used += cost
            lines.append(line)
        sources.extend(snap["sources"])
    context = HEADER + "\n".join(lines) if lines else ""
    return {"context": context, "sources": sources, "budget": budget,
            "used": counter.count(context), "unit": counter.unit}


def _preference_correction_pairs(peer, events, superseded):
    """偏好家族里「更正一下，我最爱喝美式」这类改口：旧值 → 新值的来源对。"""
    family = FAMILIES[1]
    active = {}
    pairs = {}
    for event in sorted(events, key=lambda item: item.get('recorded_at') or ''):
        if event.get('role') != 'user' or event.get('sender_id') != peer:
            continue
        if event.get('event_id') in superseded:
            continue
        try:
            parsed = family._parse(event.get('text') or '')
            if not parsed:
                continue
            kind, match, correction = parsed
            key, value = family._record(kind, match)
        except Exception:
            continue
        previous = active.get(key)
        if correction and previous and previous[1] != value:
            pairs[previous[0]] = event['event_id']
        active[key] = (event['event_id'], value)
    return pairs


def refresh_corrections(store, scope):
    'Upsert family-derived correction pairs into supersedes_map (additive).'
    events = store.events(scope)
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    peer = scope.removeprefix("qq:c2c:") if scope.startswith("qq:c2c:") else None
    if not peer:
        return 0
    pairs = FAMILIES[0].correction_pairs(peer, events, superseded)
    pairs.update(_preference_correction_pairs(peer, events, superseded))
    with store.db:
        store.db.executemany(
            "INSERT OR IGNORE INTO supersedes_map(scope,supersedes_id,event_id) VALUES(?,?,?)",
            [(scope, replaced_id, correcting_id) for replaced_id, correcting_id in pairs.items()])
    return len(pairs)


def narrative(store, scope, weeks=4):
    """A rule-built storyline of shared time: per recent week, what happened.

    Real friends can summarize "this week we..." — this aggregates the
    archive into per-week counts by family and the most distinctive topics,
    with zero model calls.
    """
    events = store.events(scope)
    if not events:
        return []
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    peer = scope.removeprefix("qq:c2c:") if scope.startswith("qq:c2c:") else None
    user_events = [event for event in events if event["role"] == "user"]
    weeks_out = []
    buckets = {}
    for event in user_events:
        day = datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00")).date()
        key = day.strftime("%G-W%V")
        buckets.setdefault(key, []).append(event)
    for key in sorted(buckets)[-max(weeks, 0):]:
        week_events = buckets[key]
        entry = {"周": key, "消息": len(week_events)}
        if peer:
            probes = (("约定", FAMILIES[2].live_states(peer, [e for e in events], superseded)[0]),)
            loops = FAMILIES[3].open_loops(peer, events, superseded)
            entry["想法"] = [loop["值"] for loop in loops]
        topics = {}
        for event in week_events:
            for term in set(re.findall(r"[\u3400-\u9fff]{2,4}", event["text"])):
                topics[term] = topics.get(term, 0) + 1
        entry["高频话题"] = [term for term, _ in sorted(topics.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
        weeks_out.append(entry)
    return weeks_out


def consolidate(store, scope, today=None):
    report = {"scope": scope}
    fresh_before = xuyi_index.fresh(store, scope)
    if not fresh_before:
        xuyi_index.rebuild(store, scope)
    report["index"] = {"fresh_before": fresh_before, "fresh_after": True,
                       "chunks": xuyi_index.chunk_count(store, scope)}
    report["corrections_mapped"] = refresh_corrections(store, scope)
    suspected = []
    events = store.events(scope)
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    peer = scope.removeprefix("qq:c2c:") if scope.startswith("qq:c2c:") else None
    if peer:
        for family in (FAMILIES[0], FAMILIES[1]):
            method = getattr(family, "suspected_pairs", None)
            if method:
                suspected.extend(method(peer, events, superseded))
    report["疑似更正待确认"] = suspected
    report["audit"] = audit(store, scope, last=200)
    report["probes"] = probes(store, scope)
    report["upcoming"] = upcoming(store, scope, today=today)
    report["open_loops"] = _open_loop_ages(store, scope)
    report["narrative"] = narrative(store, scope)
    report["archive_events"] = len(store.events(scope))
    return report


def _open_loop_ages(store, scope):
    """Open intention loops with their age in days — the natural follow-up list."""
    from datetime import datetime as _dt
    events = store.events(scope)
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    peer = scope.removeprefix("qq:c2c:") if scope.startswith("qq:c2c:") else None
    if not peer:
        return []
    now = _dt.now(_dt.fromisoformat(events[-1]["recorded_at"].replace("Z", "+00:00")).tzinfo) \
        if events else None
    rows = []
    for loop in FAMILIES[3].open_loops(peer, events, superseded):
        recorded = _dt.fromisoformat(loop["来源"]["recorded_at"].replace("Z", "+00:00"))
        age = (now - recorded).days if now else None
        rows.append({"想做的事": loop["值"], "age_days": age,
                     "event_id": loop["来源"]["event_id"]})
    return sorted(rows, key=lambda row: -(row["age_days"] or 0))


def probes(store, scope):
    """Check each rule-covered statement against the actual read path."""
    events = store.events(scope)
    best = {}
    for family_index, query in PROBES:
        family = FAMILIES[family_index]
        covered = [event for event in events
                   if event["role"] == "user" and family.covers(event["text"].strip())]
        if not covered:
            continue
        highlight = current_highlight(scope, query, events)
        sources = {event["event_id"] for event in highlight[1]} if highlight else set()
        replaced = set(highlight[2]) if highlight else set()
        for event in covered:
            if event["event_id"] in sources:
                status = "当前候选"
            elif event["event_id"] in replaced:
                status = "已被更替"
            else:
                status = "存而不达"
            row = {"family": type(family).__name__.strip("_Family").lower(),
                   "event_id": event["event_id"], "status": status,
                   "text": event["text"][:80]}
            previous = best.get(event["event_id"])
            if previous is None or STRENGTH[status] > STRENGTH[previous["status"]]:
                best[event["event_id"]] = row
    return [best[key] for key in sorted(best)]
