"""便利贴交给宿主模型写：看板与 DSH 适配器之间的文件桥。

看板是单独的本机网页，自己调不了模型；DSH 里的续忆适配器能调，但只在聊天时醒着。
两边用一个很小的目录传话（在数据库旁边的 board-bridge/）：

1. 看板每次打开某个好友的页面，把"这次要写哪几条便利贴"（中性事实 + 用户实际配置的人设）
   写成 request.json；这一份内容只写一次。
2. 适配器每隔两秒看一眼：有新的、刚写的请求，就调用宿主模型，用那个人设的口吻把每条事实
   写成一张小纸条，写进 notes.json（带同一个指纹）。失败也写一条 error 记录，隔一会儿再试。
3. 看板下次刷新读到 notes.json 且指纹对得上，就用模型写的；读不到就退回模板文案。

所以：只在看板打开且内容变化时生成一次；没有宿主、失败或超时都只是继续用模板，
聊天路径仍然零模型调用。
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRIDGE_DIR = "board-bridge"
# 纸条的写法版本：排版/提示词/校验规则改了就在这里 +1，旧纸条自动作废、重写一次。
NOTE_WRITER = 2
# 日记正文的写法版本（与纸条分开算）。
# 2：按当天聊了多少条定长度、500 字封顶（用户定：200–400 最好，500 是上限）。
# 写法版本只在这里定：请求里带上，适配器照着写回（不用改 JS、不用重启 DSH）。
DIARY_WRITER = 2
DIARY_MAX_CHARS = 500


def scope_key(scope):
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]


def paths(db_path, scope, name=None):
    """(request file, reply file) for one contact; diary passes a per-day name."""
    root = Path(db_path).resolve().parent / BRIDGE_DIR / scope_key(scope)
    if name:
        return root / f"{name}.request.json", root / f"{name}.reply.json"
    return root / "request.json", root / "notes.json"


def fingerprint(facts, persona):
    """Changes when either the facts or the configured persona change.

    ``v`` is the sticky-note wording version: bump it to invalidate notes written
    by an older prompt without touching the archived facts.
    """
    payload = json.dumps({"v": 2, "facts": facts, "persona": persona}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def note_prompt(facts):
    """Instructions for the host model; lives here so wording tweaks need no DSH restart."""
    lines = "\n".join(f"{i}. {fact['fact'].strip()}" for i, fact in enumerate(facts, 1))
    return (f"现在请你写便利贴。下面 {len(facts)} 条是你刚想起来的事。"
            "用你平时说话的口吻，一条写成一张便利贴上的短句。\n"
            "规矩：一条一张，一张一行。每张 15 到 35 个字——写清「他说的哪件事」加上「你的反应或答案」，"
            "别只写一个词、一个名字或半句话；便利贴不是发消息，可以比聊天时多说几个字。"
            "只写纸条上的话，不编号、不加引号、不解释、不寒暄。\n\n" + lines)


def read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 每次用不同的临时名：页面轮询与多个标签页可能同时写，同名临时文件在 Windows 上会撞。
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX and Windows


def valid_notes(notes, count, writer=None):
    return (isinstance(notes, list) and len(notes) == count
            and all(isinstance(n, dict) and isinstance(n.get("text"), str) and n["text"].strip()
                    and isinstance(n.get("kind"), str) for n in notes)
            and writer == NOTE_WRITER)


def _placed(notes, facts):
    """Put each model note back on the event it came from.

    适配器只写"类型 + 文字"（它不需要知道聊天在哪一天）；顺序与事实一一对应，
    所以在这里把来源补回去，日记本才能贴到正确的那一页。
    """
    out = []
    for index, note in enumerate(notes):
        fact = facts[index] if index < len(facts) else {}
        placed = dict(note)
        for key in ("event_id", "at"):
            if key in fact:
                placed[key] = fact[key]
        out.append(placed)
    return out


def request_notes(db_path, scope, facts, persona, now):
    """Return (cached model notes or None, whether a fresh request was just written)."""
    if not facts or not persona:
        return None, False
    want = fingerprint(facts, persona)
    request, reply = paths(db_path, scope)
    cached = read(reply)
    if (cached and cached.get("fingerprint") == want and not cached.get("error")
            and valid_notes(cached.get("notes"), len(facts), cached.get("writer"))):
        return _placed(cached["notes"], facts), False
    write(request, {"scope": scope, "fingerprint": want, "facts": facts, "persona": persona,
                    "prompt": note_prompt(facts), "writer": NOTE_WRITER, "at": now})
    return None, True


def weekday_of(day):
    from .diary import WEEKDAYS
    from datetime import date
    return WEEKDAYS[date.fromisoformat(day).weekday()]


def diary_fingerprint(day, messages, persona):
    payload = json.dumps({"date": day, "messages": messages, "persona": persona},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diary_length(count):
    """聊得少就写短，聊得多就写长；最长 450 字，硬上限 DIARY_MAX_CHARS。

    count 是这一天双方说的条数（取样后最多 120，超过 40 都算"聊得多"）。
    """
    if count <= 12:
        return {"min": 150, "max": 250, "paragraphs": (2, 3)}
    if count <= 40:
        return {"min": 200, "max": 350, "paragraphs": (3, 4)}
    return {"min": 300, "max": 450, "paragraphs": (3, 5)}


def diary_prompt(day, weekday, messages):
    lines = "\n".join(f"[{m['time']}] {'他' if m['role'] == 'user' else '你'}：{m['text']}"
                      for m in messages)
    size = diary_length(len(messages))
    low, high = size["paragraphs"]
    return (f"这是 {day}（{weekday}）你们当天聊过的内容，按时间顺序。\n"
            "请用你平时说话的口吻，写一篇这一天的日记。\n"
            "要求：\n"
            f"- 分成 {low} 到 {high} 段，每段两三句话，段与段之间空一行；"
            f"全文 {size['min']} 到 {size['max']} 字，无论如何不超过 {DIARY_MAX_CHARS} 字。\n"
            "- 不是流水账：只写这天里真正有内容的事（他说的事、你们的约定、他的心情、你记住的细节），"
            "寒暄、重复和没内容的话不要写。可以写细一点：他当时怎么说的、你心里怎么想。\n"
            "- 不要编：只能写这些话里出现过的事，不要加没发生过的事。\n"
            "- 像晚上自己回想这一天，用你的第一人称写，不要标题、日期、署名、列表或任何格式符号。\n\n"
            + lines)


# ---- 什么时候写：当天只贴便利贴，到了写日记的钟点（默认晚上十点，用户可在设置里改）把当天写成定稿 ----
DIARY_HOUR = 22
DIARY_HOURS = range(18, 24)   # 设置里可选：下午六点到晚上十一点
SETTINGS_FILE = "settings.json"


def diary_hour(db_path):
    """用户选的写日记钟点（存在档案旁的 settings.json）；没设或不合法时用默认。"""
    try:
        value = json.loads((Path(db_path).resolve().parent / SETTINGS_FILE).read_text(encoding="utf-8"))["diary_hour"]
    except (OSError, ValueError, KeyError, TypeError):
        return DIARY_HOUR
    return value if isinstance(value, int) and value in DIARY_HOURS else DIARY_HOUR


def set_diary_hour(db_path, hour):
    if not isinstance(hour, int) or isinstance(hour, bool) or hour not in DIARY_HOURS:
        raise ValueError(f"写日记时间只能选 {DIARY_HOURS.start}–{DIARY_HOURS.stop - 1} 点")
    path = Path(db_path).resolve().parent / SETTINGS_FILE
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    current["diary_hour"] = hour
    write(path, current)
    return hour


def diary_due(day, now=None, hour=DIARY_HOUR):
    """这一天该不该写日记了：之前的日子随时可写；今天要到钟点；以后的日子不写。"""
    now = now or datetime.now().astimezone()
    today = now.date().isoformat()
    return day < today or (day == today and now.hour >= hour)


def finalized(reply):
    """已经写成的日记就是定稿：不看写法版本、不看内容后来变没变，永不重写（用户定）。"""
    if reply and not reply.get("error") and isinstance(reply.get("text"), str) and reply["text"].strip():
        return reply["text"]
    return None


def request_diary(db_path, scope, day, messages, persona, now, allow_write=True):
    """Return (cached diary text or None, whether a fresh request was just written).

    写完就定稿：这一天已经有正文，就不再管内容变没变、也不会重写（规则升级也不重写）。
    `allow_write=False` 用于"还没到写日记的时候"——只读缓存，不请模型。
    """
    if not messages or not persona:
        return None, False
    request, reply = paths(db_path, scope, f"diary-{day}")
    done = finalized(read(reply))
    if done:
        return done, False
    if not allow_write:
        return None, False
    want = diary_fingerprint(day, messages, persona)
    write(request, {"kind": "diary", "scope": scope, "date": day, "fingerprint": want,
                    "messages": messages, "persona": persona,
                    "prompt": diary_prompt(day, weekday_of(day), messages),
                    "max_chars": DIARY_MAX_CHARS, "writer": DIARY_WRITER, "at": now})
    return None, True


def prepare_yesterday(store, db_path, scopes, persona=None, today=None):
    """新的一天第一条消息进来时，把"昨天"的日记请求写出去（一天只写一次）。

    这就是"当天结束就写日记"的触发点：不用等用户打开看板，也不用定时器。
    """
    from .diary import day_messages
    if persona is None:
        found = find_persona()
        persona = found and found["text"]
    if not persona:
        return 0
    today = today or datetime.now().astimezone().date()
    day = (today - timedelta(days=1)).isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    written = 0
    for scope in scopes:
        # 已经有定稿就不再扫库（每条消息都会走这里，先走这个快路径）。
        if finalized(read(paths(db_path, scope, f"diary-{day}")[1])):
            continue
        messages = day_messages(store, scope, day)
        if not messages:
            continue
        _, pending = request_diary(db_path, scope, day, messages, persona, now)
        written += 1 if pending else 0
    return written


def prepare_due(store, db_path, scopes, persona=None, now=None, hour=None):
    """到点写日记：补前一天（没写成的），到了钟点再写今天。适配器每隔几分钟调一次（xuyi diary-due）。"""
    now = now or datetime.now().astimezone()
    hour = diary_hour(db_path) if hour is None else hour
    if persona is None:
        found = find_persona()
        persona = found and found["text"]
    if not persona:
        return 0
    written = prepare_yesterday(store, db_path, scopes, persona=persona, today=now.date())
    if now.hour < hour:
        return written
    from .diary import day_messages
    day = now.date().isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for scope in scopes:
        if finalized(read(paths(db_path, scope, f"diary-{day}")[1])):
            continue
        messages = day_messages(store, scope, day)
        if messages:
            written += 1 if request_diary(db_path, scope, day, messages, persona, stamp)[1] else 0
    return written


def cached_diary(db_path, scope, day, messages, persona):
    """Only read an already-generated entry (导出时用；不会为导出去写请求、调模型）。

    定稿就是定稿：写日记之后当晚又聊了几句，导出照样用那篇定稿。
    """
    if not messages or not persona:
        return None
    return finalized(read(paths(db_path, scope, f"diary-{day}")[1]))


def _direct_prompt(patch):
    # 装进 DSH 的包自带纯 Python 版 PyYAML；万一缺了，只是读不到人设（看板退回模板），不能拖垮存档。
    try:
        import yaml
    except ImportError:
        return None
    try:
        entries = yaml.safe_load(Path(patch).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == "im-qqbot":
            config = entry.get("config")
            if isinstance(config, dict) and isinstance(config.get("directPrompt"), str):
                text = config["directPrompt"].strip()
                if text:
                    return text
    return None


def _patch_mtime(folder):
    try:
        return (folder / "cordis.patch.yml").stat().st_mtime
    except OSError:
        return 0.0


def find_persona(profile_root=None):
    """The persona the user actually configured for the bot, from the host's QQ plugin.

    Explicit profile dir → XUYI_PROFILE_DIR → newest ~/.dsh/profiles/*/cordis.patch.yml
    that carries an im-qqbot directPrompt. None means "no persona configured",
    and the board keeps its templates instead of inventing a voice.
    """
    roots = []
    if profile_root:
        roots.append(Path(profile_root))
    elif os.environ.get("XUYI_PROFILE_DIR"):
        roots.append(Path(os.environ["XUYI_PROFILE_DIR"]))
    else:
        base = Path.home() / ".dsh" / "profiles"
        if base.is_dir():
            roots.extend(sorted((folder for folder in base.iterdir() if folder.is_dir()),
                                key=_patch_mtime, reverse=True))
    for root in roots:
        patch = root / "cordis.patch.yml"
        if not patch.is_file():
            continue
        text = _direct_prompt(patch)
        if text:
            return {"text": text, "source": str(patch)}
    return None
