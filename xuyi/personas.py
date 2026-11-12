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
from pathlib import Path

from .store import MemoryError

ROOT = Path(__file__).resolve().parent / "personas"
REQUIRED = ("name", "label", "description", "systemPrompt")

# 不管用哪个人设（预设或自己写的），在 QQ 上发消息的样子都一样：附在人设后面一起写进宿主。
CHAT_STYLE = """在 QQ 上发消息的样子：
——像真人发消息那样，每条单独一行；换一行就是另发一条，所以一条里不要再换行。发几条看当时的情形，不是固定的数：大多数时候一条就够；有两层意思、或者说完又想补一句的，发两条；真的兴奋、要吐槽、或一件事得说几句，才发三条，最多一到三条。别连着几次都发同样的条数，回头看看你前几轮发了几条，这次换一个。
——条数的感觉是这样（只看条数，不看内容）：
他：「在吗」→ 你：「在」（一条）
他：「今天又加班」→ 你：「啧」「吃饭没」（两条）
他：「我中奖了！！五十块」→ 你：「真的假的」「请客请客」「别想赖」（三条）
——每条一般不超过二十来个字。他说得少你也说得少，他就回一个字的时候，你一条就够。
——不用任何排版符号：不加粗、不用星号、不写标题、不列清单、不用代码块。表情偶尔一个就行。
——不是每次都要追问，话说完了就停；也不用每次都接住他所有的话，挑一个接。
——对话的分寸：他一句话你别回五行；别老反问；提意见最多一条，他没问就别给。
——你不是只会接话的人：有时自己起个话头，说说你刚想到的、刚碰到的事；想起他之前提过还没下文的事，就顺口问一句进展；偶尔一句话没说完，再补一条。但一轮最多主动起一个话头，他没接就算了，别追着问。
——他认真问你是不是 AI、是不是真人，别骗他，照实说；玩笑式的问法可以接着玩。"""

# 群聊（宿主的 groupPrompt）：同一个人设，加上群里的规矩。消息每行是「[昵称 (编号)] 内容」。
GROUP_STYLE = """在 QQ 群里说话的样子：
——群里有好几个人。每条消息前面的「[昵称 (一串编号)]」是谁说的；叫人用昵称，绝不把那串编号念出来。
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
    """宿主里现在配的是哪个人设：预设名/「自定义人设」/没配。给首次引导显示用。"""
    from .board_bridge import find_persona
    found = find_persona(profile_root)
    group = group_reply_status(profile_root)
    if not found:
        return {"configured": False, "name": None, "label": None, "preview": "", "group_reply": group}
    text = found["text"]
    for item in list_personas():
        if compose_prompt(load_persona(item["name"])["systemPrompt"]) == text:
            return {"configured": True, "name": item["name"], "label": item["label"],
                    "preview": item["description"], "group_reply": group}
    body = text.split(CHAT_STYLE)[0].strip() if CHAT_STYLE in text else text.strip()
    return {"configured": True, "name": None, "label": "自定义人设", "preview": body[:80], "group_reply": group}


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
        data = {"label": "自定义人设", "systemPrompt": text}
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
