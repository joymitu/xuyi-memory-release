"""Replay a narrow set of current-memory candidates from archived QQ text.

Three statement families (name, preference, appointment) share one contract:
at most one unambiguous candidate per query, every candidate carries its
verbatim archived source, and competing values without an explicit change
marker abort the automatic path.
"""
from datetime import datetime
import re

from .phrasing import HOLE, variants


PEER = re.compile(r"[0-9a-f]{64}")
HISTORY_QUERY = re.compile(r"曾经|原来|原名|原先|以前|最初|旧名|过去|改名前|改名之前|之前叫|上次叫|当时|那时候")
OTHER_OWNER = re.compile(r"朋友|同事|同学|邻居|别人|他家|她家|你家|你的猫|你的狗")
UNCERTAIN = re.compile(r"什么|吗|么|吧|来着|或者|还是|不是|并不|可能|大概|暂定|之一|之类|好像|记得|哈哈|真的")
VALUE = r"(?P<value>[\u3400-\u9fffA-Za-z0-9·_-]{1,20})"


def _eligible(event):
    raw = event["text"]
    return (event["role"] == "user" and event["origin"] == "quoted" and event["sender_id"] is not None
            and event["event_id"].startswith("dsh:") and len(raw) <= 80 and "\n" not in raw and "\r" not in raw)


def _archived_user_lines(events, superseded):
    for event in events:
        if not _eligible(event) or event["event_id"] in superseded:
            continue
        # 原话之外再给出口语改写后的标准句式；来源仍是这条原话。
        for text in variants(event["text"]):
            yield event, text


def relevant(event):
    """Could any family ever read this event? Decides membership of the highlight index.

    Must be a superset of every line a family looks at, or indexed recall would
    diverge from the full scan (parity is tested).
    """
    if "supersedes" in event:
        return True
    return _eligible(event) and any(family.relevant(text) for text in variants(event["text"])
                                    for family in FAMILIES)


def _by_event_time(pair):
    return datetime.fromisoformat(pair[0]["recorded_at"].replace("Z", "+00:00"))


class _NameFamily:
    QUERY = re.compile(r"叫(?:我)?(?:什么|啥|哪个)|名字|昵称|称呼")
    PET = r"宠物|小?猫|小?狗|兔子|仓鼠|※"
    STATEMENT = re.compile(
        r"(?:(?P<correction>更正一下|更正|刚才说错了)[，,:：]\s*|(?:请)?记住[，,:：]\s*)?"
        r"(?:(?:我(?:家|的)的?(?P<pet>" + PET + r")(?:的名字是|叫))|(?P<nickname>请叫我|以后叫我|我希望你叫我))"
        + VALUE + r"[。！!]?"
    )
    UNCERTAIN_VALUE = UNCERTAIN
    PET_QUERY = re.compile(PET)
    NICKNAME_QUERY = re.compile(r"叫我|称呼我|我的(?:名字|昵称|称呼)")

    def relevant(self, text):
        return self.covers(text)

    def covers(self, text):
        match = self.STATEMENT.fullmatch(text)
        return bool(match and not self.UNCERTAIN_VALUE.search(match["value"]))

    def _statements(self, peer, events, superseded):
        """[(key, event, match)] in archive order; a ※ pet means "the pet named most recently"."""
        out = []
        last_pet = None
        for event, text in _archived_user_lines(events, superseded):
            if event["sender_id"] != peer:
                continue
            match = self.STATEMENT.fullmatch(text)
            if not match or self.UNCERTAIN_VALUE.search(match["value"]):
                continue
            pet = match["pet"]
            key = pet.removeprefix("小") if pet else "用户称呼"
            if key == HOLE:
                if last_pet is None:
                    continue
                key = last_pet
            elif pet:
                last_pet = key
            if any(item[1] is event and item[0] == key and item[2]["value"] == match["value"] for item in out):
                continue
            out.append((key, event, match))
        return out

    def suspected_pairs(self, peer, events, superseded):
        'Competing values with no explicit correction — NEEDS_REVIEW candidates.'
        candidates = {}
        for key, event, match in self._statements(peer, events, superseded):
            candidates.setdefault(key, []).append((event, match))
        pairs = []
        for statements in candidates.values():
            statements.sort(key=_by_event_time)
            corrections = [event for event, match in statements if match["correction"]]
            if corrections:
                continue
            values = {}
            for event, match in statements:
                values.setdefault(match["value"], event)
            if len(values) > 1:
                ordered = sorted(values.items(), key=_by_event_time_kv if False else
                                 lambda kv: kv[1]["recorded_at"])
                pairs.append({"家族": "name", "旧值": ordered[0][0],
                              "旧来源": ordered[0][1]["event_id"],
                              "新值": ordered[-1][0], "新来源": ordered[-1][1]["event_id"]})
        return pairs

    def correction_pairs(self, peer, events, superseded):
        'Deterministic {replaced_id: correcting_id} pairs from the archive.'
        candidates = {}
        for key, event, match in self._statements(peer, events, superseded):
            candidates.setdefault(key, []).append((event, match))
        pairs = {}
        for statements in candidates.values():
            statements.sort(key=_by_event_time)
            active = {}
            for event, match in statements:
                if match["correction"]:
                    active.clear()
                active[match["value"]] = event
            if len(active) != 1:
                continue
            final = next(iter(active))
            for event, match in statements:
                if match["value"] != final:
                    pairs[event["event_id"]] = active[final]["event_id"]
        return pairs

    def highlight(self, peer, query, events, superseded):
        pets = {match[0].removeprefix("小") for match in self.PET_QUERY.finditer(query)}
        nickname = self.NICKNAME_QUERY.search(query)
        if bool(pets) == bool(nickname):
            return None
        candidates = {}
        for key, event, match in self._statements(peer, events, superseded):
            pet = key != "用户称呼"
            if (pet and not (key in pets or "宠物" in pets)) or (not pet and not nickname):
                continue
            candidates.setdefault(key, []).append((event, match))
        if len(candidates) != 1:
            return None
        key, statements = next(iter(candidates.items()))
        statements.sort(key=_by_event_time)
        active = {}
        for event, match in statements:
            if match["correction"]:
                active.clear()
            active[match["value"]] = event
        if len(active) != 1:
            return None
        value, source = next(iter(active.items()))
        replaced = {event["event_id"] for event, match in statements if match["value"] != value}
        return {"属性": key if nickname else key + "名字", "值": value}, [source], replaced


class _PreferenceFamily:
    QUERY = re.compile(
        r"最(?:爱|喜欢|讨厌)(?:[^。？！?]*?)?(?:什么|啥)"
        r"|(?:爱|喜欢|讨厌)(?:[喝吃玩看听用穿去])(?:[^。？！?]{0,6})?(?:什么|啥)"
        r"|还(?:在|再)?(?:[喝吃玩用去])(?:[^。？！?]{0,8})?(?:吗|么)|戒了(?:吗|没有)"
    )
    CLASS_OF = {"喝": "饮品", "饮料": "饮品", "饮品": "饮品",
                "吃": "食物", "食物": "食物", "菜": "食物",
                "零食": "零食", "水果": "水果", "玩": "游戏", "游戏": "游戏",
                "看": "影音", "电影": "影音", "剧": "影音",
                "听": "音乐", "歌": "音乐", "歌曲": "音乐", "音乐": "音乐",
                "用": "用品", "穿": "穿着", "去": "去处", "书": "阅读",
                "颜色": "颜色", "运动": "运动", "花": "花", "动物": "动物"}
    NOUNS = ("饮料", "饮品", "食物", "零食", "水果", "游戏", "电影", "影音", "音乐",
             "歌曲", "歌", "书", "颜色", "运动", "花", "动物", "用品", "穿着", "去处", "阅读", "菜")
    VERBS = "喝吃玩看听用穿去"
    PREFIX = re.compile(r"(?:从今天起|从现在起|现在|以后|近期|最近|另外|对了)[，,：:]?\s*")
    CORRECTION = re.compile(r"^(?:请)?更正(?:一下)?[，,：:]?\s*|刚才?说错[了]?[，,：:]?\s*")
    PAST = re.compile(r"以前|曾经|原来|小时候|之前|上次")
    CAT = r"(?P<cat>[\u3400-\u9fffA-Za-z0-9※]{1,6})"
    SUPER = re.compile(r"我最(?:爱|喜欢|讨厌)的?" + CAT + r"是" + VALUE + r"[。！!]?")
    CHANGE = re.compile(r"我最(?:爱|喜欢|讨厌)的?" + CAT + r"(?:改|换)(?:成|为|到)" + VALUE + r"了?[。！!]?")
    VERB = re.compile(r"我最(?:爱|喜欢|讨厌)(?P<verb>[喝吃玩看听用穿去])" + VALUE + r"[。！!]?")
    NEGATE_VERB = re.compile(r"我(?:不再|不)(?P<verb>[喝吃玩看听用穿去])" + VALUE + r"了?[。！!]?")
    NEGATE_BARE = re.compile(r"我戒了" + VALUE + r"[。！!]?")
    TRIM = "了的吧嘛呢啊呀哦嗯哈哟"

    def highlight(self, peer, query, events, superseded):
        target = self._query_key(query)
        parsed = []
        for event, text in _archived_user_lines(events, superseded):
            if event["sender_id"] != peer:
                continue
            item = self._parse(text)
            if item:
                parsed.append((event, *item))
        parsed.sort(key=_by_event_time)
        active = {}
        suspected = []
        for key, statements in self._history(parsed).items():
            seen = []
            current = None
            conflicted = False
            replaced = set()
            last = None
            for event, kind, rendered, correction in statements:
                strong = correction or kind in ("change", "negate")
                if strong:
                    replaced |= {item[0]["event_id"] for item in seen if item[1] != rendered}
                    current = (event, rendered)
                    conflicted = False
                elif current is None and not conflicted:
                    current = (event, rendered)
                elif current and current[1] == rendered:
                    current = (event, rendered)
                elif current and current[1] != rendered:
                    conflicted = True
                    suspected.append({"家族": "preference", "旧值": current[1],
                                      "旧来源": current[0]["event_id"], "新值": rendered,
                                      "新来源": event["event_id"]})
                    current = None
                last = (event, rendered)
                seen.append((event, rendered))
            if conflicted or current is None:
                continue
            active[key] = {"属性": self._label(key), "值": current[1],
                           "来源": current[0], "replaced": replaced,
                           "疑似": suspected[-1] if suspected and suspected[-1]["新来源"] == last[0]["event_id"] else None}
        keys = [key for key in active if target is None or key == target]
        if target is None:
            plain = [key for key in keys if not isinstance(key, tuple)]
            keys = plain or keys
        if len(keys) != 1:
            return None
        slot = active[keys[0]]
        return {"属性": slot["属性"], "值": slot["值"]}, [slot["来源"]], slot["replaced"]

    def _history(self, parsed):
        history = {}
        for event, kind, key, rendered, correction in self._resolved(parsed):
            history.setdefault(key, []).append((event, kind, rendered, correction))
        return history

    def _resolved(self, parsed):
        """(event, kind, key, rendered, correction); a ※ category means the one named most recently."""
        last = None
        seen = set()
        for event, kind, match, correction in parsed:
            key, rendered = self._record(kind, match)
            if not rendered or UNCERTAIN.search(rendered):
                continue
            if key == HOLE:
                if last is None:
                    continue
                key = last
            elif not isinstance(key, tuple):
                last = key
            if (id(event), key, rendered) in seen:
                continue
            seen.add((id(event), key, rendered))
            yield event, kind, key, rendered, correction

    def relevant(self, text):
        return self.covers(text)

    def covers(self, text):
        return self._parse(text) is not None

    def suspected_pairs(self, peer, events, superseded):
        'Conflicting values with no explicit correction — NEEDS_REVIEW candidates.'
        parsed = []
        for event, text in _archived_user_lines(events, superseded):
            if event["sender_id"] != peer:
                continue
            item = self._parse(text)
            if item:
                parsed.append((event, *item))
        parsed.sort(key=_by_event_time)
        suspected = []
        for event, kind, match, correction in parsed:
            _key, rendered = self._record(kind, match)
            if kind in ("change", "negate") and not correction:
                continue  # explicit change resolves; nothing suspected
        # 重放时间线，收集无标记冲突点
        history = self._history(parsed)
        for key, statements in history.items():
            current = None
            open_conflict = None
            for position, (event, kind, rendered, correction) in enumerate(statements):
                strong = correction or kind in ("change", "negate")
                if strong:
                    current = (event, rendered)
                    open_conflict = None  # a later explicit correction resolves it
                elif current and current[1] != rendered:
                    open_conflict = {"家族": "preference", "旧值": current[1],
                                     "旧来源": current[0]["event_id"], "新值": rendered,
                                     "新来源": event["event_id"]}
                    suspected.append(open_conflict)
                    current = None
                elif current is None or current[1] == rendered:
                    current = (event, rendered)
                if open_conflict and any(
                        stmt[3] or stmt[1] in ("change", "negate")
                        for stmt in statements[position + 1:]):
                    suspected.remove(open_conflict)
                    open_conflict = None
        return suspected

    def _query_key(self, query):
        noun = next((n for n in self.NOUNS if n in query), None)
        verb = next((v for v in self.VERBS if v in query), None)
        return self.CLASS_OF.get(noun or verb) if (noun or verb) else None

    def _parse(self, text):
        correction = bool(self.CORRECTION.match(text))
        body = self.CORRECTION.sub("", text, count=1)
        body = self.PREFIX.sub("", body, count=1)
        if self.PAST.search(body):
            return None
        for kind, pattern in (("change", self.CHANGE), ("super", self.SUPER), ("verb", self.VERB),
                              ("negate", self.NEGATE_VERB), ("negate", self.NEGATE_BARE)):
            match = pattern.fullmatch(body)
            if match:
                return kind, match, correction
        return None

    def _record(self, kind, match):
        if kind in ("super", "change"):
            # "我最喜欢喝的改成…" 的类别是动词"喝"，不是名词"喝的"。
            cat = match["cat"].removesuffix("的")
            key = self.CLASS_OF.get(cat, cat)
            return key, self._trim(match["value"])
        if kind == "verb":
            return self.CLASS_OF[match["verb"]], self._trim(match["value"])
        verb = match.groupdict().get("verb")
        if verb:
            return self.CLASS_OF[verb], "不再" + verb + self._trim(match["value"])
        value = self._trim(match["value"])
        return ("戒", value), value

    def _label(self, key):
        if isinstance(key, tuple):
            return "已戒"
        return "最喜欢的" + key

    def _trim(self, value):
        return value.rstrip(self.TRIM)


class _AppointmentFamily:
    QUERY = re.compile(
        r"(?:有什么|啥)(?:约定|安排)"
        r"|(?:约定|安排|赴约|约会|邀约|见面|碰面)(?:[^。？！?]{0,12})?(?:吗|么|啥|什么|哪)"
        r"|还(?:要|得)?(?:去|见|来)(?:不)?(?:吗|么)|见不见|去不去"
    )
    TIME = r"(?:(?:周|星期)[一二三四五六日天]|\d{1,2}[号日]|\d{1,2}点(?:半)?|明天|后天|今天|大后天|下?周末|下?(?:周|星期)[一二三四五六日天])"
    WHO = r"[\u3400-\u9fffA-Za-z0-9·_-]{1,10}"
    MAKE = re.compile(r"(?:约(?:好|了)?[，,]?)?(?P<time>" + TIME + r")(?:一起)?(?:见|约|陪|找)(?P<who>" + WHO + r")"
                      r"|(?:和|跟|与)(?P<who2>" + WHO + r")[^。！!]{0,4}(?P<time2>" + TIME + r")")
    CANCEL = re.compile(r"(?P<time>" + TIME + r")?(?:见(?P<who>" + WHO + r"))?的?(?:约定|安排|约会|邀约|约)(?:[^。！!]{0,4})?(?:取消|作废|不去了|去不了|吹了)"
                        r"|(?:取消|作废)了?(?:跟|和|与)?(?P<who2>" + WHO + r")的?(?:约定|安排|约)"
                        r"|(?P<time2>" + TIME + r")[^。！!]{0,6}(?:不去了|去不了|取消|作废|爽约)")
    BARE_CANCEL = re.compile(r"^(?:请)?(?:取消|作废)了?[。！!]?$")
    APPOINTMENT_NOUN = re.compile(r"约定|安排|约会|邀约")
    MOVE = re.compile(r"(?:改到|改成|推迟到|提前到|延到|换到)(?:了)?(?P<new>" + TIME + r")")
    QUERY_WHO = re.compile(r"[和跟同与](?P<who>" + WHO + r")的")
    TENTATIVE = re.compile(r"打算|暂定|可能|大概|好像|说不定|如果|要是|再说|吧")
    NOT_A_WHO = {"时间", "空", "机会", "空余", "我", "你", "他", "她", "它", "人", "大家", "我们",
                 "定", "会", "期", "好", "了", "饭", "面"}
    MARKER = re.compile(r"改到|改成|推迟到|提前到|延到|换到|取消|作废|不去了|去不了|爽约|吹了")

    def highlight(self, peer, query, events, superseded):
        qwho = self.QUERY_WHO.search(query)
        qwho = qwho.group("who") if qwho else None
        qtime = re.search(self.TIME, query)
        qtime = qtime.group(0) if qtime else None
        slots, ambiguous = self.live_states(peer, events, superseded)
        if ambiguous:
            return None
        matched = [slot for slot in slots
                   if (not qwho or slot["who"] == qwho)
                   and (not qtime or self._time_eq(qtime, slot["time"])
                        or slot.get("new") == qtime)]
        if len(matched) != 1:
            return None
        slot = matched[0]
        if slot["status"] == "planned":
            record = {"属性": f"和{slot['who']}的约定", "值": slot["time"]}
            sources = [slot["make"]]
        else:
            value = f"{slot['time']}，已取消" if slot["status"] == "cancelled" else f"{slot['time']}改到{slot['new']}"
            record = {"属性": f"和{slot['who']}的约定", "值": value}
            sources = [slot["last"], slot["make"]]
        return record, sources, set(slot["replaced"])

    def live_states(self, peer, events, superseded):
        """Replay make/cancel/move statements in event order into appointment slots.

        Returns (slots, ambiguous); ambiguous marks an unresolvable cancel or
        move, which makes the whole family abstain.
        """
        parsed = []
        for event, text in _archived_user_lines(events, superseded):
            if event["sender_id"] != peer:
                continue
            item = self._parse(text)
            if item:
                parsed.append((event, *item))
        parsed.sort(key=_by_event_time)
        appts = {}
        order = []
        for event, kind, fields in parsed:
            if kind == "make":
                key = (fields["who"], fields["time"])
                if key not in appts:
                    appts[key] = {"who": fields["who"], "time": fields["time"], "status": "planned",
                                  "make": event, "last": event, "replaced": set()}
                    order.append(key)
                continue
            targets = [key for key in order if appts[key]["status"] in ("planned", "moved")
                       and (not fields["who"] or key[0] == fields["who"])
                       and (not fields["time"] or key[1] == fields["time"])]
            if len(targets) > 1:
                return [], True
            if not targets:
                continue
            slot = appts[targets[0]]
            slot["last"] = event
            slot["replaced"].add(slot["make"]["event_id"])
            if kind == "cancel":
                slot["status"] = "cancelled"
            else:
                slot["status"] = "moved"
                slot["new"] = fields["new"]
        return [appts[key] for key in order], False

    @staticmethod
    def _time_eq(query_time, appt_time):
        weekend = ("周六", "周日", "周天")
        return query_time == appt_time or (query_time == "周末" and appt_time in weekend) \
            or (appt_time == "周末" and query_time in weekend)

    def relevant(self, text):
        return self.covers(text)

    def covers(self, text):
        return self._parse(text) is not None

    def _parse(self, text):
        if self.TENTATIVE.search(text):
            return None
        if self.BARE_CANCEL.fullmatch(text):
            return "cancel", {"who": None, "time": None}
        move = self.MOVE.search(text)
        marker_at = self.MARKER.search(text)
        head = text[:marker_at.start()] if marker_at else text
        if move:
            make = self.MAKE.search(head)
            who = None
            time = None
            if make:
                who = make["who"] or make["who2"]
                time = make["time"] or make["time2"]
            if not who:
                owner = re.search(r"[和跟同与](?P<who>" + self.WHO + r")", head)
                who = owner.group("who") if owner else None
            if not time:
                found = re.search(self.TIME, head)
                time = found.group(0) if found else None
            # Neither who nor time ("钓鱼改到周日了"): like a bare cancel, it may only
            # resolve against a single live appointment; several make it ambiguous.
            return "move", {"who": who, "time": time, "new": move.group("new")}
        cancel = self.CANCEL.search(text)
        if cancel:
            who = cancel.group("who") or cancel.group("who2")
            who = who.rstrip("的") if who else None
            time = cancel.group("time") or cancel.group("time2")
            if not who and not time:
                if self.BARE_CANCEL.fullmatch(text):
                    return "cancel", {"who": None, "time": None}
                if not self.APPOINTMENT_NOUN.search(text):
                    return None
                owner = re.search(r"[和跟同与](?P<who>" + self.WHO + r")的", text)
                who = owner.group("who") if owner else None
                return "cancel", {"who": who, "time": None}
            return "cancel", {"who": who, "time": time}
        make = self.MAKE.search(text)
        if make:
            who = make["who"] or make["who2"]
            if who in self.NOT_A_WHO:
                return None
            time = make["time"] or make["time2"]
            return "make", {"who": who, "time": time}
        return None


class _OpenLoopFamily:
    """Intentions without a fixed date (考试/想买的东西/在准备的事) that the
    companion can naturally follow up on. Narrow on purpose: one explicit
    make statement per loop, explicit closes, and a generic query only
    answers when exactly one loop is open."""
    QUERY = re.compile(
        r"(?:最近|接下来)(?:有什么|啥)(?:打算|计划)"
        r"|(?:有什么|啥)(?:打算|计划)(?:吗|么|没)"
        r"|(?:要|想|打算|准备)(?:做|干|办)的?什么(?:来着)?"
        r"|准备得?(?:怎么样|如何|咋样)"
    )
    OBJECT = r"(?P<object>[\u3400-\u9fffA-Za-z0-9·_-]{2,12})"
    MAKE = re.compile(r"我(?:(?:最近|下周|下个月|月底前?|明天|后天|过(?:几天|阵子)|年底)[，,]?)?"
                      r"(?:要|打算|想|准备|在)" + OBJECT + r"[。！!]?")
    CLOSE = re.compile(OBJECT + r"(?:考完了?|买到了?|做完了?|办完了?|结束了?|泡汤了|不做了|不搞了|不了|放弃了?)"
                       r"|不(?:考|买|去|看|学|换|办|做了?)了")
    NOT_AN_OBJECT = {"疯", "疯了", "死", "哭", "吐", "家", "回家", "吃饭", "睡觉", "聊天",
                     "你", "你了", "你啊", "它们", "手机", "电脑"}
    TRIM = "了的吧嘛呢啊呀哦嗯哈哟"

    def _trim(self, value):
        return value.rstrip(self.TRIM)

    def highlight(self, peer, query, events, superseded):
        loops = self.open_loops(peer, events, superseded)
        mentioned = [loop for loop in loops
                     if any(gram in loop["值"] for gram in self._query_grams(query))]
        targets = mentioned or loops
        if len(targets) != 1:
            return None
        loop = targets[0]
        return {"属性": "想做的事", "值": loop["值"]}, [loop["来源"]], set()

    SENSITIVE = ("医院", "药", "病", "手术", "体检", "化疗", "去世", "葬礼", "吊唁",
                 "贷款", "欠", "工资", "存款", "赔偿")

    def open_loops(self, peer, events, superseded, include_sensitive=False):
        """All currently open intention loops, in event order. Sensitive
        topics (health/grief/finance) stay archived but out of the proactive
        surfaces unless explicitly asked for."""
        parsed = []
        for event, text in _archived_user_lines(events, superseded):
            if event["sender_id"] != peer:
                continue
            made = self.MAKE.fullmatch(text.strip())
            if made and made["object"] not in self.NOT_AN_OBJECT:
                parsed.append((event, "make", self._trim(made["object"])))
                continue
            closed = self.CLOSE.search(text.strip())
            if closed:
                parsed.append((event, "close", self._trim(closed["object"] or "")))
        parsed.sort(key=_by_event_time)
        loops = []
        for event, kind, object in parsed:
            if kind == "make":
                loops.append({"值": object, "来源": event, "closed": False})
                continue
            for loop in reversed(loops):
                if not loop["closed"] and self._overlap(loop["值"], object):
                    loop["closed"] = True
                    break
        rows = [loop for loop in loops if not loop["closed"]]
        if not include_sensitive:
            rows = [loop for loop in rows
                    if not any(word in loop["值"] for word in self.SENSITIVE)]
        return rows

    def relevant(self, text):
        return self.covers(text) or bool(self.CLOSE.search(text.strip()))

    def covers(self, text):
        text = text.strip()
        made = self.MAKE.fullmatch(text)
        return bool(made and made["object"] not in self.NOT_AN_OBJECT)

    @staticmethod
    def _overlap(first, second):
        if not first or not second:
            return False
        short, long = sorted((first, second), key=len)
        if short in long:
            return True
        grams = {short[i:i+2] for i in range(len(short)-1)} if len(short) > 1 else {short}
        return any(gram in long for gram in grams)

    @staticmethod
    def _query_grams(query):
        return {query[i:i+2] for i in range(len(query)-1)}


class _EmotionFamily:
    """Explicit self-reported feelings ("我今天好难过"). Deliberately kept out
    of the session snapshot — feelings are the sensitive tier: remembered,
    answerable when asked, never proactively raised."""
    QUERY = re.compile(
        r"我(?:今天|昨天|这几天|最近)?(?:心情|感觉)(?:怎么样|如何|咋样)"
        r"|我(?:开不开心|难不难过|累不累|烦不烦)"
        r"|你还?记得我(?:今天|昨天)?(?:心情|感觉|状态)吗"
        r"|我(?:今天|昨天)(?:是不是|是不是有?点)?(?:开心|难过|生气|累|烦)"
    )
    FEELING = r"(?P<value>开心|高兴|难过|伤心|生气|愤怒|累|疲惫|烦|烦躁|兴奋|紧张|焦虑|委屈|感动|平静)"
    STATEMENT = re.compile(r"我(?:今天|昨天|这几天|最近)?(?:真的|超|好|太|特别|非常|有点)?"
                           + FEELING + r"(?:了)?(?:[，,]?(?:因为|由于).*)?[。！!]?")
    HEDGE = re.compile(r"可能|好像|大概|也许|说不定")

    def highlight(self, peer, query, events, superseded):
        parsed = []
        for event, text in _archived_user_lines(events, superseded):
            if event["sender_id"] != peer:
                continue
            match = self.STATEMENT.fullmatch(text.strip())
            if match and not self.HEDGE.search(text):
                parsed.append((event, match["value"]))
        if not parsed:
            return None
        parsed.sort(key=_by_event_time)
        source, value = parsed[-1]
        return {"属性": "心情", "值": value}, [source], set()

    def relevant(self, text):
        return self.covers(text)

    def covers(self, text):
        match = self.STATEMENT.fullmatch(text.strip())
        return bool(match and not self.HEDGE.search(text))


class _DietFamily:
    """Food allergies and restrictions ("我对芒果过敏", "我不能吃辣", "我吃素").

    Health-adjacent, so it follows the sensitive tier: never in the session
    snapshot or any proactive surface. It is recalled only when the turn itself
    is about eating or drinking — the moment forgetting it could hurt. Several
    restrictions can hold at once; all live ones (max 4) come back with sources.
    """
    QUERY = re.compile(
        r"过敏|忌口|(?:能|可以)不?(?:能)?(?:吃|喝)|不能(?:吃|喝)|(?:吃|喝)(?:点|些)?(?:什么|啥)"
        r"|推荐.{0,6}(?:吃|喝|菜|外卖|餐|饭|零食|水果|甜品)|外卖|(?:早|午|晚)(?:饭|餐)|早点|夜宵|做(?:什么|啥)菜"
    )
    # Explicit diet words win over the preference family's "吃什么"-style questions.
    STRONG = re.compile(r"过敏|忌口|(?:能|可以)不?(?:能)?(?:吃|喝)|不能(?:吃|喝)")
    # 不含"对/我"：否则"我妈对花生过敏"会把"妈对花生"当成过敏原。
    ITEM = r"(?P<item>[^\s对我，,。！!？?]{1,10}?)"
    RESTRICT = (
        ("allergy", re.compile(r"我?(?:是)?(?:还|也)?(?:对)?" + ITEM + r"过敏(?:了)?")),
        ("cannot", re.compile(r"我(?:还|也)?(?:不能|没法|不敢)(?P<verb>吃|喝|碰)" + ITEM)),
        ("cannot", re.compile(r"我(?:还|也)?(?P<verb>吃|喝)不了" + ITEM)),
        ("avoid", re.compile(r"我(?:还|也)?不(?P<verb>吃|喝)" + ITEM)),
        ("intolerant", re.compile(r"我(?:还|也)?" + ITEM + r"不耐受")),
        ("vegetarian", re.compile(r"我(?:一直|现在|已经)?(?:在)?(?:吃素|是素食(?:者|主义者)?)(?:好几年了|很多年了|很久了|了)?")),
    )
    LIFT = (
        re.compile(r"我?(?:现在|已经)?(?:能|可以)(?:吃|喝)(?:一点|点)?" + ITEM + r"了"),
        re.compile(r"我?(?:现在|已经)?(?:对)?" + ITEM + r"不过敏了"),
        re.compile(r"我?(?:不再|没有)对" + ITEM + r"过敏了?"),
    )
    NOT_THIS = re.compile(r"不是" + ITEM)
    HEDGE = re.compile(r"可能|好像|大概|也许|说不定|是不是|会不会|应该")
    END = "。！!，,~～了的"

    def _parse(self, text, raw):
        from .phrasing import CORRECTION, HYPOTHETICAL, OTHERS, PAST, QUESTION
        if QUESTION.search(raw) or OTHERS.search(raw) or HYPOTHETICAL.search(raw) or PAST.search(raw) \
                or self.HEDGE.search(raw):
            return None
        body = text.strip().rstrip("。！!，,~～")
        for pattern in self.LIFT:
            if match := pattern.fullmatch(body):
                return "lift", self._item(match)
        if CORRECTION.search(raw) and (match := self.NOT_THIS.fullmatch(body)):
            return "lift", self._item(match)
        for kind, pattern in self.RESTRICT:
            if kind == "avoid" and body.endswith("了"):
                continue  # "我不吃香菜了" is a taste change (preference family), not a restriction
            if match := pattern.fullmatch(body):
                if kind == "allergy" and not body.startswith("我") and "我" not in raw:
                    continue  # subjectless "酒精过敏" only inside a first-person sentence
                if kind == "vegetarian":
                    return "restrict", ("素食", "吃素")
                item = self._item(match)
                if not item:
                    continue
                verb = match.groupdict().get("verb") or "吃"
                rendered = {"allergy": f"对{item}过敏", "cannot": f"不能{verb}{item}",
                            "avoid": f"不{verb}{item}", "intolerant": f"{item}不耐受"}[kind]
                return "restrict", (item, rendered)
        return None

    def _item(self, match):
        return match["item"].strip(self.END)

    @staticmethod
    def _same(first, second):
        return first == second or (len(first) >= 1 and len(second) >= 1 and (first in second or second in first))

    def highlight(self, peer, query, events, superseded):
        live = {}
        for event, text in _archived_user_lines(events, superseded):
            if event["sender_id"] != peer:
                continue
            parsed = self._parse(text, event["text"])
            if not parsed:
                continue
            kind, value = parsed
            if kind == "restrict":
                item, rendered = value
                live[item] = (event, rendered)
            else:
                for item in [known for known in live if self._same(known, value)]:
                    del live[item]
        if not live:
            return None
        chosen = list(live.values())[-4:]
        sources = []
        for event, _rendered in chosen:
            if event not in sources:
                sources.append(event)
        return {"属性": "忌口/过敏", "值": "；".join(rendered for _event, rendered in chosen)}, sources, set()

    def relevant(self, text):
        return self.covers(text) or bool(self.NOT_THIS.fullmatch(text.strip().rstrip("。！!，,")))

    def covers(self, text):
        return self._parse(text, text) is not None


# Order matters: other modules address families by index; new families go last.
FAMILIES = (_NameFamily(), _PreferenceFamily(), _AppointmentFamily(), _OpenLoopFamily(),
            _EmotionFamily(), _DietFamily())


def current_highlight(scope, query, events):
    """Return at most one relevant candidate, its sources, and replaced source IDs."""
    if not query.strip() or not scope.startswith("qq:c2c:") or HISTORY_QUERY.search(query) \
            or OTHER_OWNER.search(query):
        return None
    peer = scope.removeprefix("qq:c2c:")
    if not PEER.fullmatch(peer):
        return None
    matched = [family for family in FAMILIES if family.QUERY.search(query)]
    diet = FAMILIES[5]
    if len(matched) > 1 and diet in matched:
        # "我最喜欢吃什么" is a taste question; only explicit diet words make it a diet one.
        matched = [diet] if diet.STRONG.search(query) else [family for family in matched if family is not diet]
    if len(matched) != 1:
        return None
    superseded = {event["supersedes"] for event in events if "supersedes" in event}
    return matched[0].highlight(peer, query, events, superseded)
