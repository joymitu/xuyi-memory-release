"""说法归一：把口语化的一句话改写成重点家族规则能识别的标准句式。

只做识别前的改写——档案里的原话不变，证据永远引用原话。零模型调用。
改写只在明确的句式上发生；问句、转述/他人、假设、过去、犹豫的话一律不改写
（原样交给家族规则，它们本来就会拒绝）。

省略主语的改口（"不对，是叫奶盖"、"现在最爱的是草莓了"）改写成带占位符 ※ 的
标准句，由家族在回放时指向"最近提到的那只宠物/那个类别"；之前没提过就作废。
"""
import re

HOLE = "※"
VALUE = r"([㐀-鿿A-Za-z0-9·_-]{1,12})"
# 后面还跟可选尾巴（就行/一点/我）时取最短，免得把尾巴吞进值里。
LAZY = r"([㐀-鿿A-Za-z0-9·_-]{1,12}?)"
TIME = (r"(?:(?:周|星期)[一二三四五六日天]|\d{1,2}[号日]|明天|后天|今天|大后天|下?周末"
        r"|下?(?:周|星期)[一二三四五六日天])")

QUESTION = re.compile(r"[？?]|(?:吗|呢|么)[。！!～~\s]*$|什么|啥|怎么|哪")
OTHERS = re.compile(r"(?:我妈|我爸|爸爸|妈妈|男朋友|女朋友|男票|女票|老公|老婆|朋友|同事|同学|邻居|室友|他|她|你)"
                    r"(?:家|的|说|最|超|特别|很|也|都|觉得|对|不|是|吃|喝)")
HYPOTHETICAL = re.compile(r"如果|要是|假如|假设|的话")
PAST = re.compile(r"小时候|以前|曾经|上周|上个月|去年|那时候")
TENTATIVE = re.compile(r"可能|大概|打算|要不要|也许|说不定|吧|想不想|看情况")
CORRECTION = re.compile(r"更正|不对|记错|说错|打错|搞错|改名|算了还是|不叫|不是[^，,。]{1,8}[，,]")

PETS = (("橘猫", "猫"), ("狸花猫", "猫"), ("小猫咪", "猫"), ("猫咪", "猫"), ("小猫", "猫"), ("猫", "猫"),
        ("狗狗", "狗"), ("小狗", "狗"), ("狗", "狗"), ("兔子", "兔子"), ("仓鼠", "仓鼠"))
PET_WORD = "|".join(word for word, _ in PETS)
VERBS = "喝吃听看玩"

LEAD = re.compile(r"^(?:不过|而且|然后|所以|其实|对了|嗯+|那|哈哈+|诶|欸)[，,]?")
TAIL = re.compile(r"(?:哈+|呀|啦|啊|哦|嘛|呢|~+|～+)+$")
SPLIT = re.compile(r"[，,。！!；;～~\s]+")

NAME = re.compile(r"^我(?:家|的)的?(?:那只|这只|那条|这条)?(" + PET_WORD + r")的?(?:名字)?(?:是|叫)" + VALUE + "$")
ADOPT = re.compile(r"^我(?:家)?(?:养了|有)(?:一)?(?:只|条)?(" + PET_WORD + ")$")
BARE_NAME = re.compile(r"^(?:现在|是|就|它)?(?:改)?叫" + VALUE + "$")
NICK = re.compile(r"^(?:算了)?(?:还是)?(?:你)?(?:以后|就|可以|就可以)?(?:叫|喊)我" + LAZY + r"(?:就行|就好|就可以|吧)?$")  # 只有祈使的"叫我X吧"里"吧"不表示猜测
LIKE = re.compile(r"^我(?:平时|一直|现在)?(?:超级|超|特别|真的|很|最)*(?:爱|喜欢)([" + VERBS + "])" + VALUE + "$")
CRUSH = re.compile(r"^(?:最近|现在)?迷上了?" + VALUE + "$")
TOPIC_NEG = re.compile(r"^" + LAZY + r"我?(戒了|不([" + VERBS + r"])了)$")
NEG = re.compile(r"^我?(?:最近|现在)?不(?:爱|再)?([" + VERBS + "])" + VALUE + "了$")
SWITCH = re.compile(r"^(?:现在|最近|以后)?(?:改|换)(?:成|为)?([" + VERBS + "])?" + VALUE + "了?$")
FAVOURITE_NOW = re.compile(r"^(?:现在)?(?:最爱|最喜欢)的是" + VALUE + "了?$")
PREFER_NOW = re.compile(r"^(?:现在)?更(?:爱|喜欢)" + LAZY + r"(?:一点|一些)?了?$")
WHO = r"([㐀-鿿A-Za-z]{1,4}?)"
ACTION = r"(?=约好|约了|一起|去|开会|吃饭|聚餐|聚|打|逛|看|见|碰|钓|爬|玩|喝|吃|唱|出去|$)"
CLOCK = r"(?:早上|上午|中午|下午|晚上)?(?:\d{1,2}点半?|[一二三四五六七八九十]{1,3}点半?)?"
MEET_TIME_FIRST = re.compile(r"^(?:这|这个)?(" + TIME + ")" + CLOCK + r"(?:我)?(?:要|得)?(?:和|跟|与|约了|约)" + WHO + ACTION)
MEET_WHO_FIRST = re.compile(r"^(?:我)?(?:约了|约好了?|和|跟|与)" + WHO + r"(?:这|这个)?(" + TIME + ")")
CALL_OFF = re.compile(r"^(?:跟|和|与)?" + WHO + r"(?:那顿饭|那个约|那事|的约|那边|[^，,。]{0,4}的事)(?:取消|吹|黄)了$")


def _pet(word):
    return next(canonical for raw, canonical in PETS if raw == word)


def _verb(text):
    found = re.search("[" + VERBS + "]", text)
    return found.group(0) if found else None


def _clauses(text):
    out = []
    for part in SPLIT.split(text):
        part = TAIL.sub("", LEAD.sub("", part.strip()))
        if part:
            out.append(part)
    return out


def _rewrite(clause, context):
    """One clause → a canonical sentence, or None when no rule applies."""
    fix = "更正一下，" if context["correction"] else ""
    if clause.endswith("吧") and not NICK.fullmatch(clause):
        return None  # "我的猫叫甲号吧"是猜测，不是陈述
    if match := NAME.fullmatch(clause):
        return f"{fix}我家的{_pet(match[1])}叫{match[2]}。"
    if match := NICK.fullmatch(clause):
        return f"{fix}以后叫我{match[1]}。"
    if match := BARE_NAME.fullmatch(clause):
        if context["pet"]:
            return f"{fix}我家的{context['pet']}叫{match[1]}。"
        if fix:
            return f"{fix}我家的{HOLE}叫{match[1]}。"
        return None
    if match := LIKE.fullmatch(clause):
        return f"我最喜欢{match[1]}{match[2]}。"
    if (match := CRUSH.fullmatch(clause)) and context["verb"]:
        return f"我最喜欢{context['verb']}{match[1]}。"
    if match := TOPIC_NEG.fullmatch(clause):
        return f"我戒了{match[1]}。" if match[2] == "戒了" else f"我不{match[3]}{match[1]}了。"
    if match := NEG.fullmatch(clause):
        return f"我不{match[1]}{match[2]}了。"
    if (match := SWITCH.fullmatch(clause)) and "到" not in clause[:3]:
        verb = match[1] or context["verb"]
        if verb and not re.search(TIME, clause):
            return f"我最喜欢{verb}的改成{match[2]}了。"
    if match := FAVOURITE_NOW.fullmatch(clause):
        return f"我最爱的{HOLE}改成{match[1]}了。"
    if match := PREFER_NOW.fullmatch(clause):
        return f"我最喜欢的{HOLE}改成{match[1]}了。"
    if not context["tentative"]:
        if match := MEET_TIME_FIRST.match(clause):
            return f"我约好{match[1]}见{match[2]}。"
        if match := MEET_WHO_FIRST.match(clause):
            return f"我约好{match[2]}见{match[1]}。"
        if not re.search(TIME, clause) and (match := CALL_OFF.fullmatch(clause)):
            return f"见{match[1]}的约定取消了。"
    return None


def variants(text):
    """Candidate sentences for the family rules: the original first, then rewrites.

    When a clause is rewritten, its rewrite replaces it; unrewritten clauses of a
    multi-clause sentence are offered on their own, so trailing chatter
    ("……，超级粘人") no longer hides a statement.
    """
    text = text.strip()
    if not text or QUESTION.search(text) or OTHERS.search(text) or HYPOTHETICAL.search(text) \
            or PAST.search(text):
        return [text]
    clauses = _clauses(text)
    pet = next((_pet(found.group(0)) for found in re.finditer(PET_WORD, text)), None)
    context = {"correction": bool(CORRECTION.search(text)), "pet": pet, "verb": _verb(text),
               "tentative": bool(TENTATIVE.search(text))}
    rewritten = [_rewrite(clause, context) for clause in clauses]
    if not any(rewritten):
        return [text] + ([clause + "。" for clause in clauses] if len(clauses) > 1 else [])
    out = []
    for clause, new in zip(clauses, rewritten):
        candidate = new or clause + "。"
        if candidate not in out:
            out.append(candidate)
    return out
