"""Run an explicitly configured extractor against a bounded recent evidence window."""
import json
import os
import subprocess

from .embeddings import ByteCounter
from .states import States
from .store import MemoryError, canonical


def extract(store, scope, command, budget=12000, counter=None, timeout=60, max_output_tokens=512):
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
        raise MemoryError("提取命令须为非空的参数数组")
    if type(budget) is not int or budget < 1 or type(max_output_tokens) is not int or max_output_tokens < 1:
        raise MemoryError("提取预算须为正整数")
    counter = counter or ByteCounter()
    states = States(store)
    base = states.view(scope)
    events = store.events(scope)
    payload = {"scope": scope, "messages": [], "max_output_tokens": max_output_tokens}
    for event in reversed(events):
        selected = [event, *payload["messages"]]
        new_payload = {**payload, "messages": selected}
        if counter.count(canonical(new_payload)) > budget:
            break
        payload = new_payload
    if not payload["messages"]:
        raise MemoryError("没有可提取的消息，或最新消息超过预算")
    request = canonical(payload)
    try:
        result = subprocess.run(command, input=request, capture_output=True, text=True,
                                encoding="utf-8", timeout=timeout, check=False,
                                # 协议是 UTF-8 字节流；Windows 上 Python 子进程默认按代码页解码
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MemoryError("提取器启动失败或超过等待时间，原文和旧状态已保留") from exc
    if result.returncode:
        raise MemoryError(f"提取器退出码为 {result.returncode}，原文和旧状态已保留")
    if len(result.stdout.encode()) > 65536:
        raise MemoryError("提取器输出超过原型限制，原文和旧状态已保留")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MemoryError("提取器未返回合法 JSON，原文和旧状态已保留") from exc
    supplied = {event["event_id"] for event in payload["messages"]}
    if not isinstance(records, list) or any(
        not isinstance(record, dict) or not isinstance(record.get("sources"), list) or any(
            not isinstance(source, dict) or not isinstance(source.get("event_id"), str) or source["event_id"] not in supplied for source in record["sources"]
        ) for record in records
    ):
        raise MemoryError("提取候选引用了本次未提供的消息，或格式无效")
    candidate = {"through_id": payload["messages"][-1]["event_id"],
                 "extractor": "command/recent-window", "records": records}
    state = states.propose(scope, candidate, base["revision"]) if records else states.view(scope)
    return {"status": "candidate_written" if records else "empty_candidate", "state": state,
            "input_used": counter.count(request), "input_budget": budget, "input_unit": counter.unit,
            "messages_supplied": len(payload["messages"]), "archived_messages": len(events),
            "output_bytes": len(result.stdout.encode()), "requested_max_output_tokens": max_output_tokens}
