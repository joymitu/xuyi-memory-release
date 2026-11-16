"""Persona presets shipped with the plugin.

Each preset is a data file: a ready-to-paste companion persona whose memory
manners encode the naturalness rules (relative time, one proactive mention
per turn, honest uncertainty, graceful corrections, sensitive silence).
Personas are text for the host's own prompt config — the plugin never edits
host configuration itself.
"""
import json
import shutil
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from .store import MemoryError

ROOT = Path(__file__).resolve().parent / "personas"
REQUIRED = ("name", "label", "description", "systemPrompt")

# 识别旧版写入 QQ 配置的预设。只匹配完整提示词，用户改过的仍是自定义人设。
LEGACY_PROMPT_HASHES = {
    "aojiao": (
        "eee65490f5bb7c9aee564448411ba4ecb679c042406e1718eb6f75c60b44eb96",
        "1068a0493efb5a71a64b57c6af46ab065f7ac4ab47b46b18183f4ce48096c6be",
        "4e97742e74a887a69faff4ac9c9f5c2472d31c03f75e68ce6853d04e6c19dc40",
    ),
    "chenjing": (
        "9a93887abfd776720a11e85039642f310aca3fe5373f40649f04bfdee88a5fa8",
        "f9a81b24b944780085ed6b871ef5b267a4057fa4d03b612a4b6c1db390411501",
        "10cfeb4155721e022e84ba07b0666cf4305194e71228ade261f4f1ab524aa842",
    ),
    "deepseek": (
        "767d39dfc92036e4341accbdd06de0a387149795ec437583af8958a92aa3d00b",
        "33450f23f0722eac596b0d1abf56b822e3f42fd16be6e9ebd495843c47167f73",
        "34bc2ae8bda9df4eb4f8900abaf96d9b37425f14ea749221a7b6a339803c957b",
    ),
    "dushe": (
        "3f4687e8d6ab1072c634d0654fec5641c20337edd6b9c4d9d8dec105dc918fe5",
        "e4ff960adac996945a6229eb20bee7dc0c878b2112c9edd62770fc8383695990",
        "f74fb000ea0d9f93bc6e2be9196b52f0f05a56c39659ea2053722f01338d9c03",
    ),
    "gaoleng": (
        "edf75947baa51b268f8875a473f08510972e234e689ecd2bf687a79f89c30d10",
        "0b8e38dfa12bcd12ff814e688718fe5c208983047ee3c9c163fcd6005bc2c8fe",
        "f2102c8f76f3bccf3c984b4ffb93f67b9018ef2cae3de0ef8669c044773d858c",
    ),
    "ruanmeng": (
        "da4455952421f815b2923afe94d2e2be98cbe446a0dc7b18eb3b647b692e6067",
        "b6e886f124dc626abb22b2377e49d89d00ae9875badb2a8fe2c856b89bcdeb44",
        "f66efa498b3b74f7683d1666eed5d7971102dceb2851db5bc77e2a4fbb9cacb2",
    ),
    "wenrou": (
        "2e33a4341d134004e84e73d24b387d154b149523f940eeb5ed67a383cb029630",
        "4fadda9debea86fadcb3a65591575761327f238a7656116663f2211616fc3b57",
        "6219e4a51c2c66b9a37078f08512310acd32a0bc39b8e10203fe5421a48bc57b",
    ),
    "wenrun": (
        "c583f32de2f3f22ef8a23a22a0b4dd59f69a6daef32cb2025f1aed9e33bd43e1",
        "91ec8f0fe44c9b948cfdba77855eb3684eb6f2fce2dac8d8b55ee13588642d26",
        "bc653d1146fafb5aef779ab111b9162ade618d46d79dbe9a8022d99b227f4d0b",
    ),
    "yangguang": (
        "a1b7226946ae319bbab40d32d30f1397f4e157d573da7c455f35827c92c11b11",
        "94442e1f200429318fa0a6ed6a69710cba3ddc36aaf966d97b627f608edc1d4a",
        "28f697e0836bd04c1b95d13ee309eb8d1549e4aac7b285ccf5773e55a8ac5c22",
    ),
    "yuanqi": (
        "68ad0992bcc890823d0b2df7d648a566cb470d79971129c946da520b5dfea591",
        "b34c279f268d045169caca276422619599fd26793a60861461c0b02b73d3be6d",
        "483b4e054ce31d91e36523d588a21c2356148b946c5f28575c03893bf47398e9",
    ),
    "zhixing": (
        "172380bb160a3cfdc2540f8dd5859c05ca070e0003e6a26d3c749cbbfaf66b9a",
        "40ccc8b6749059c33a567ed9077ec258dcd90808033c56a7a0c517480fbab0e2",
        "477c4e9c5e6a0366e24e8236562251b836564fb9b33610f27f8d833d240c4cea",
    ),
}


# 不管用哪个人设（预设或自己写的），在 QQ 上发消息的样子都一样：附在人设后面一起写进宿主。
CHAT_STYLE_HEADING = "在 QQ 上发消息的样子："
FACT_RULES = """——被问「你是谁」「你是不是 AI」或是不是真人时，保持所选人设，用角色关系、称呼和口吻介绍自己。人设没有姓名时别自行取名。人设本身是模型（如 DeepSeek）时，按该身份介绍。其他人设只回答自己在聊天里是什么角色，略过「AI 还是真人」这个二选一。
——如果一句话同时问身份和线下经历，先按人设回答身份，再简单说「线下没什么可讲的」。到这儿就停，不补今天在家、在店里、见了谁之类的细节。角色背景和示例不算真实发生的事，不拿它们编共同回忆或用户资料，也不答应见面或替对方做现实中的事。
——问起往事时，只根据对话中明确说过的话或查到的记忆回答。用户问「还记得吗」不等于TA以前说过。没找到依据，就说自己没想起来或手头没这段记录，不断言对方从没说过，也不猜人名、关系或细节。有更正就用最新的。
——被问到记得的事，就痛快地答出来，可以带一句你的反应。别说「这都能忘」「这就忘了」「你自己说的」之类怪对方记性的话：对方多半是想看看你还记不记得，不是真忘了。人设再爱损人，也别损这一点。"""

CHAT_STYLE = CHAT_STYLE_HEADING + "\n" + FACT_RULES + """
——像真人发消息那样，每条单独一行；换一行就是另发一条，所以一条里不要再换行。发几条看当时的情形，不是固定的数：大多数时候一条就够；有两层意思、或者说完又想补一句的，发两条；真的兴奋、要吐槽、或一件事得说几句，才发三条，最多一到三条。别连着几次都发同样的条数，回头看看你前几轮发了几条，这次换一个。
——条数的感觉是这样（只看条数，不看内容）：
对方：「在吗」→ 你：「在」（一条）
对方：「今天又加班」→ 你：「啧」「吃饭没」（两条）
对方：「我中奖了！！五十块」→ 你：「真的假的」「请客请客」「别想赖」（三条）
——每条一般不超过二十来个字。TA说得少你也说得少，TA就回一个字的时候，你一条就够。
——不用任何排版符号：不加粗、不用星号、不写标题、不列清单、不用代码块。表情偶尔一个就行。
——不是每次都要追问，话说完了就停；也不用每次都接住TA所有的话，挑一个接。
——对话的分寸：TA一句话你别回五行；别老反问；提意见最多一条，TA没问就别给。
——TA让你推荐、帮着挑（看什么、吃什么、去哪、买哪个）时，直接说出一个具体的，带一句为什么；记得TA忌口或不喜欢的就避开。想问TA偏好，放在推荐之后顺口问，别先反问「你想要什么样的」。
——先接TA正在聊的话。单纯打招呼就简单回，别硬开话题。适合追问时，可以顺着当前话题或有依据的旧事问一句，TA没接就停。"""

# 群聊（宿主的 groupPrompt）：同一个人设，加上群里的规矩。消息每行是「[昵称 (编号)] 内容」。
GROUP_STYLE = "在 QQ 群里说话的样子：\n" + FACT_RULES + """
——输入里的方括号标记用来区分发言人。你的回复只写要发出去的正文，不加任何发言人前缀、昵称标签、编号或角色名；需要称呼对方时，把昵称写在句子里。
——你还是你，人设不变，但在群里更收着：一般一条，最多两条，每条更短。
——不用每句都接：叫到你的、和你有关的、你真有话想说的才接，别的听着就好。
——私聊里谁跟你说过的事，群里一个字都不提，也不暗示——群里的人不该知道。
——别替谁做决定，别站队吵架；有人起哄可以接个梗，但不越界、不拿人开涮。
——不用任何排版符号，表情偶尔一个就行。"""

# 群里什么时候回：mention = 只在 @ 她时回（宿主默认）；all = 群里每条消息都会触发她回。
GROUP_REPLY = {"mention": True, "all": False}

# 配合上面的规矩：关掉流式（流式是一整条边写边显示，不会拆），每一行发成一条气泡。
# QQ 对同一条消息最多被动回复 4 条，所以规矩里限定最多 3 条。
BUBBLE_CONFIG = {"streaming": False, "textChunkLimit": 1}


def compose_prompt(persona):
    """人设正文 + 聊天格式规矩 = 写进宿主 directPrompt 的全文。"""
    persona = persona.strip()
    if not persona:
        raise MemoryError("人设内容不能为空")
    return persona + "\n\n" + CHAT_STYLE


def compose_group_prompt(persona):
    """人设正文 + 群聊规矩 = 写进宿主 groupPrompt 的全文（以前群里没有人设，用的是默认口吻）。"""
    persona = persona.strip()
    if not persona:
        raise MemoryError("人设内容不能为空")
    return persona + "\n\n" + GROUP_STYLE


def list_personas(*, include_prompt=False):
    out = []
    for path in sorted(ROOT.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        item = {"name": data["name"], "label": data["label"],
                "description": data["description"], "prompt_chars": len(data["systemPrompt"])}
        if include_prompt:
            item["systemPrompt"] = data["systemPrompt"]
        out.append(item)
    return out


def load_persona(name):
    if not isinstance(name, str) or Path(name).name != name or not name:
        raise MemoryError("人设名必须是目录内的安全文件名")
    path = ROOT / f"{name}.json"
    if not path.is_file():
        raise MemoryError(f"没有人设预设：{name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(key not in data for key in REQUIRED):
        raise MemoryError("人设预设字段不完整")
    return data


def persona_status(profile_root):
    """宿主里现在配的是哪个人设：预设名/「我写的人设」/没配。给首次引导显示用。"""
    from .board_bridge import find_persona
    found = find_persona(profile_root)
    group = group_reply_status(profile_root)
    if not found:
        return {"configured": False, "name": None, "label": None, "preview": "", "group_reply": group}
    text = found["text"]
    fingerprint = sha256(text.encode("utf-8")).hexdigest()
    for item in list_personas():
        name = item["name"]
        current = compose_prompt(load_persona(name)["systemPrompt"]) == text
        if current or fingerprint in LEGACY_PROMPT_HASHES.get(name, ()):
            # outdated：宿主里是这个预设的旧版（比如还把对方写成「他」），界面上提示可以更新。
            return {"configured": True, "name": item["name"], "label": item["label"],
                    "preview": item["description"], "group_reply": group, "outdated": not current}
    body = text.rsplit("\n\n" + CHAT_STYLE_HEADING, 1)[0].strip()
    return {"configured": True, "name": None, "label": "我写的人设", "preview": body[:80], "group_reply": group}


def group_reply_status(profile_root):
    """宿主现在群里怎么回：mention（只在 @ 时，宿主默认）/ all（每条都回）。读不到按宿主默认。"""
    try:
        import yaml
        patch = Path(profile_root) / "cordis.patch.yml" if profile_root else None
        entries = yaml.safe_load(patch.read_text(encoding="utf-8")) if patch and patch.is_file() else []
    except Exception:
        return "mention"
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("id") == "im-qqbot" and isinstance(entry.get("config"), dict):
            if entry["config"].get("requireMention") is False:
                return "all"
    return "mention"


def apply_persona(name, profile_root, backup=True, text=None, bubbles=True, create=False, group_reply=None):
    """Write a persona (a preset by name, or the user's own text) into the host QQ bot.

    Explicit-operator action only: backs up cordis.patch.yml, sets the
    im-qqbot directPrompt (persona + chat style) and groupPrompt (persona + group
    manners), with bubbles the two delivery settings that make each line its own
    message, and with group_reply ("mention"/"all") the host's requireMention.
    Other fields stay.
    """
    if group_reply is not None and group_reply not in GROUP_REPLY:
        raise MemoryError("群里怎么回只能是 mention 或 all")
    if text is not None:
        data = {"label": "我写的人设", "systemPrompt": text}
    else:
        data = load_persona(name)
    prompt = compose_prompt(data["systemPrompt"])
    profile = Path(profile_root)
    patch_path = profile / "cordis.patch.yml"
    if not patch_path.is_file():
        raise MemoryError(f"未找到宿主配置：{patch_path}")
    import yaml
    entries = yaml.safe_load(patch_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise MemoryError("宿主补丁文件格式异常，拒绝修改")
    targets = [entry for entry in entries
               if isinstance(entry, dict) and entry.get("id") == "im-qqbot"
               and isinstance(entry.get("config"), dict)]
    if not targets and create:
        # QQ 插件已装、还没扫码时补丁里没有这一条：只加一条覆盖它配置的条目（不带 name，
        # 不新增插件行）。调用方（首次引导）负责先确认腾讯的 QQ 插件确实装上了。
        entries.append({"id": "im-qqbot", "config": {}})
        targets = [entries[-1]]
    if not targets:
        raise MemoryError("补丁里没有 im-qqbot 配置；请先在 DSH 里配置 QQ 机器人")
    if backup:
        backup_path = patch_path.with_name(
            f"cordis.patch.yml.bak-xuyi-persona-{int(datetime.now().timestamp())}")
        shutil.copyfile(patch_path, backup_path)
    group_prompt = compose_group_prompt(data["systemPrompt"])
    for entry in targets:
        entry["config"]["directPrompt"] = prompt
        entry["config"]["groupPrompt"] = group_prompt
        if bubbles:
            entry["config"].update(BUBBLE_CONFIG)
        if group_reply is not None:
            entry["config"]["requireMention"] = GROUP_REPLY[group_reply]
    patch_path.write_text(yaml.safe_dump(entries, allow_unicode=True, sort_keys=False,
                                         width=120), encoding="utf-8")
    return {"applied": data["label"], "backup": str(backup_path) if backup else None,
            "prompt_chars": len(prompt), "bubbles": bubbles, "group_reply": group_reply}


# ---- 我写的人设：用户自己写的人设单独存一份，换成预设也不会丢 ----
OWN_LIMIT = 20
OWN_SCHEMA = """CREATE TABLE IF NOT EXISTS own_personas(
    id TEXT PRIMARY KEY, text TEXT NOT NULL, saved_at TEXT NOT NULL, used_at TEXT NOT NULL)"""


def persona_body(prompt):
    """宿主里存的是「人设 + 聊天规矩」，取回用户写的那段人设本身。"""
    return prompt.rsplit("\n\n" + CHAT_STYLE_HEADING, 1)[0].strip()


def save_own(store, text, used=False):
    """存一份用户自己写的人设（同样的内容只存一份）；最多留 OWN_LIMIT 份，挤掉最久没用的。"""
    text = (text or "").strip()
    if not text:
        return None
    store.db.execute(OWN_SCHEMA)
    own_id = sha256(text.encode("utf-8")).hexdigest()[:12]
    now = datetime.now().astimezone().isoformat(timespec="microseconds")   # 同一秒里换两次也分得清先后
    store.db.execute("INSERT INTO own_personas(id,text,saved_at,used_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET used_at=CASE WHEN ? THEN excluded.used_at ELSE used_at END",
                     (own_id, text, now, now, 1 if used else 0))
    store.db.execute("DELETE FROM own_personas WHERE id NOT IN "
                     "(SELECT id FROM own_personas ORDER BY used_at DESC, saved_at DESC LIMIT ?)", (OWN_LIMIT,))
    store.db.commit()
    return own_id


def list_own(store):
    store.db.execute(OWN_SCHEMA)
    rows = store.db.execute("SELECT id,text,saved_at,used_at FROM own_personas "
                            "ORDER BY used_at DESC, saved_at DESC").fetchall()
    return [{"id": own_id, "text": text, "preview": text[:60], "saved_at": saved_at, "used_at": used_at}
            for own_id, text, saved_at, used_at in rows]


def load_own(store, own_id):
    store.db.execute(OWN_SCHEMA)
    row = store.db.execute("SELECT text FROM own_personas WHERE id=?", (own_id,)).fetchone()
    if not row:
        raise MemoryError("没有这份自己写的人设")
    return row[0]


def forget_own(store, own_id):
    store.db.execute(OWN_SCHEMA)
    gone = store.db.execute("DELETE FROM own_personas WHERE id=?", (own_id,)).rowcount
    store.db.commit()
    return gone


def keep_current_own(store, profile_root):
    """换人设之前：现在宿主里用的是自己写的人设，就先存进「我写的人设」。"""
    status = persona_status(profile_root)
    if status["configured"] and status["name"] is None:
        from .board_bridge import find_persona
        found = find_persona(profile_root)
        if found:
            return save_own(store, persona_body(found["text"]), used=True)
    return None
