"""Offline missed-memory audit: report recent user messages the automatic
highlight path skipped, with the rule that blocked them. Report only — the
audit never promotes, rewrites or deletes anything (the neuron_audit idea;
noise is kept out at write time, the audit shows what slipped past the
narrow rules so the rules can be widened deliberately).
"""
from .highlights import FAMILIES

TRIGGERS = ("叫我", "名字", "昵称", "称呼", "最爱", "最喜欢", "最讨厌", "戒了",
            "约定", "安排", "见小", "更正", "改到", "改成", "取消")


def audit(store, scope, last=200):
    events = store.events(scope)
    recent = events[-max(int(last), 0):]
    skipped = []
    for event in recent:
        if event["role"] != "user" or not any(trigger in event["text"] for trigger in TRIGGERS):
            continue
        reason = _skip_reason(event)
        if reason:
            skipped.append({"event_id": event["event_id"], "recorded_at": event["recorded_at"],
                            "reason": reason, "text": event["text"][:80]})
    return {"scope": scope, "scanned": len(recent), "triggered": len(skipped), "skipped": skipped}


def _skip_reason(event):
    text = event["text"]
    if event["origin"] != "quoted":
        return "来源非QQ私聊归档消息"
    if len(text) > 80 or "\n" in text or "\r" in text:
        return "复合或超长消息，自动规则跳过"
    if not any(family.covers(text.strip()) for family in FAMILIES):
        return "句型不在当前重点规则内"
    return None
