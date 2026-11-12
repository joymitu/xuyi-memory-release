"""本机记忆看板服务：xuyi board。

只监听 127.0.0.1；每次启动生成随机口令，页面与接口都要带上它（本机其他网页拿不到）。
这一版只读：GET / 页面，GET /api/contacts，GET /api/board?scope=…。

便利贴会请宿主模型按用户实际配置的人设来写：页面打开时写一份请求到数据库旁的
board-bridge/，DSH 里的适配器看到后调用模型并把纸条写好（见 board_bridge.py）。
读不到就继续用模板；看板本身永远不阻塞、不报错。
"""
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, quote, urlparse

from .board import board, contacts
from .board_bridge import cached_diary, diary_due, diary_hour, find_persona, request_diary, request_notes
from .diary import day_messages, diary

RESTORE_LIMIT = 64 * 1024 * 1024   # 导入备份的上限；其它写接口仍是 4KB
from .export import TITLE, docx, markdown
from .store import Store


def make_handler(db_path, token, persona=None):
    # 内置的手写字体（站酷快乐体，OFL 1.1，见 xuyi/fonts/OFL.txt）：不联网、不依赖系统装没装华文行楷。
    font = resources.files("xuyi").joinpath("fonts/ZCOOLKuaiLe-Regular.ttf").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 不把查询写进终端

        def page(self):
            # 每次请求都读模板：改样式/文案刷新页面即可生效，不用重启看板。
            return resources.files("xuyi").joinpath("board.html").read_text(encoding="utf-8")

        def send(self, status, body, kind, cache="no-store", extra=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            given = query.get("t", [""])[0] or self.headers.get("X-Xuyi-Token", "")
            if not secrets.compare_digest(given, token):
                return self.send(403, "需要看板口令", "text/plain; charset=utf-8")
            if url.path not in ("/api/override", "/api/restore"):
                return self.send(404, "{}", "application/json")
            # 口令已经校验；浏览器/适配器不一定原样带上 Content-Type，
            # 这里只要求 body 是 JSON（下面解析失败会 400）。
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.send(400, "{}", "application/json")
            if length > (RESTORE_LIMIT if url.path == "/api/restore" else 4096):
                return self.send(413, "{}", "application/json")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if url.path == "/api/restore":
                    # 导入备份（换电脑带走）：整份校验，不对就整份拒绝。
                    from .carry import restore_backup
                    with Store(db_path) as store:
                        result = restore_backup(store, body)
                    return self.send(200, json.dumps(result, ensure_ascii=False), "application/json")
                scope = body.get("scope")
                event_id = body.get("event_id")
                if not isinstance(scope, str) or not isinstance(event_id, str):
                    raise ValueError("要 scope 和 event_id")
                from .strength import rebuild, set_override
                with Store(db_path) as store:
                    result = set_override(store, scope, event_id, pinned=body.get("pinned"),
                                          muted=body.get("muted"), wrong=body.get("wrong"))
                    rebuild(store, [scope])
                return self.send(200, json.dumps(result, ensure_ascii=False), "application/json")
            except Exception as exc:
                return self.send(400, json.dumps({"error": str(exc)}, ensure_ascii=False), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/font/ZCOOLKuaiLe.ttf":
                # 固定内置资源，没有聊天内容；页面里不必带口令。
                return self.send(200, font, "font/ttf", "public, max-age=31536000, immutable")
            given = query.get("t", [""])[0] or self.headers.get("X-Xuyi-Token", "")
            if not secrets.compare_digest(given, token):
                return self.send(403, "需要看板口令（请用 xuyi board 打印的链接打开）", "text/plain; charset=utf-8")
            if url.path == "/":
                return self.send(200, self.page(), "text/html; charset=utf-8")
            if url.path == "/api/shutdown":
                # DSH 托管时由适配器调用。Windows 虚拟环境的 python.exe 只是启动器，杀掉它
                # 真正的看板进程还活着；让看板自己退出才靠得住。
                self.send(200, "{}", "application/json")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None
            try:
                with Store(db_path) as store:
                    if url.path == "/api/contacts":
                        out = contacts(store)
                    elif url.path == "/api/board":
                        scope = query.get("scope", [""])[0]
                        if not scope.startswith("qq:c2c:"):
                            return self.send(400, "{}", "application/json")
                        out = board(store, scope)
                        notes, pending = request_notes(
                            db_path, scope, out.get("note_facts") or [],
                            persona and persona["text"],
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
                        if notes:
                            out["notes"] = notes
                        out["notes_source"] = "model" if notes else "template"
                        out["notes_pending"] = pending
                    elif url.path == "/api/diary":
                        scope = query.get("scope", [""])[0]
                        if not scope.startswith("qq:c2c:"):
                            return self.send(400, "{}", "application/json")
                        book = board(store, scope)
                        notes, pending = request_notes(
                            db_path, scope, book.get("note_facts") or [],
                            persona and persona["text"],
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
                        out = diary(store, scope, notes=notes or book["notes"])
                        out["notes_source"] = "model" if notes else "template"
                        out["notes_pending"] = pending
                        day = query.get("date", [""])[0]
                        if day:
                            today = datetime.now().astimezone().date().isoformat()
                            hour = diary_hour(db_path)
                            # 到了用户设的钟点（默认晚上十点）才写今天；之前的日子随时补写。写完定稿。
                            text, waiting = request_diary(
                                db_path, scope, day, day_messages(store, scope, day),
                                persona and persona["text"],
                                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                allow_write=diary_due(day, hour=hour))
                            out["diary_text"] = text
                            out["diary_source"] = "model" if text else "none"
                            out["diary_pending"] = waiting
                            out["diary_today"] = day == today
                            out["diary_hour"] = hour
                    elif url.path == "/export":
                        scope = query.get("scope", [""])[0]
                        kind = query.get("format", ["md"])[0]
                        if not scope.startswith("qq:c2c:") or kind not in ("md", "docx"):
                            return self.send(400, "{}", "application/json")
                        book = diary(store, scope)
                        for day in book["days"]:
                            day["diary_text"] = cached_diary(
                                db_path, scope, day["date"],
                                day_messages(store, scope, day["date"]),
                                persona and persona["text"])
                        center = board(store, scope)["center"]
                        if kind == "md":
                            body, mime = markdown(book, center), "text/markdown; charset=utf-8"
                        else:
                            body, mime = docx(book, center), (
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                        name = f"{TITLE}.{kind}"
                        fallback = f"xuyi-diary.{kind}"
                        return self.send(200, body, mime, extra={
                            "Content-Disposition":
                                f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"})
                    elif url.path == "/api/backup":
                        # 换电脑 / 重装时带走：整本档案 + 「TA的记忆」里的开关，一个文件。
                        from .carry import backup
                        stamp = datetime.now().strftime("%Y%m%d")
                        body = json.dumps(backup(store), ensure_ascii=False)
                        return self.send(200, body, "application/json; charset=utf-8", extra={
                            "Content-Disposition": f"attachment; filename=\"xuyi-backup-{stamp}.json\"; "
                                                   f"filename*=UTF-8''{quote(f'续忆备份-{stamp}.json')}"})
                    elif url.path == "/api/selfcheck":
                        scope = query.get("scope", [""])[0]
                        if not scope.startswith("qq:c2c:"):
                            return self.send(400, "{}", "application/json")
                        from .carry import self_check
                        out = self_check(store, scope)
                    else:
                        return self.send(404, "{}", "application/json")
            except Exception as exc:  # 看板只读，出错时把原因给页面
                return self.send(500, json.dumps({"error": str(exc)}, ensure_ascii=False),
                                 "application/json; charset=utf-8")
            return self.send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")

    return Handler


def _alive(pid):
    """父进程还在不在（Windows 用 OpenProcess，其余 kill(pid, 0)）。"""
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT：还没退出
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_parent(server, pid, interval=2.0):
    # DSH 托管的看板：DSH 一退出（包括崩溃）就跟着关，不留占端口的孤儿进程。
    def loop():
        while _alive(pid):
            time.sleep(interval)
        server.shutdown()
    threading.Thread(target=loop, daemon=True).start()


def serve(db_path, port=0, open_browser=True, ready=None, profile_root=None, announce=False,
          parent_pid=None):
    token = secrets.token_urlsafe(18)
    persona = find_persona(profile_root)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path, token, persona))
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    if parent_pid:
        _watch_parent(server, parent_pid)
    if announce:
        # 给 DSH 适配器读的一行：端口和口令只在两个进程之间传，不进页面地址。
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}", "token": token}),
              flush=True)
    elif ready:
        ready(server, url)
    else:
        print(f"记忆看板：{url}\n（只在本机可用；按 Ctrl+C 关闭）", flush=True)
        if persona:
            print(f"便利贴会用你自己配置的人设来写（{persona['source']}）；DSH 没开时用现成文案。", flush=True)
        else:
            print("没找到宿主里配置的人设，便利贴用现成文案（不影响看板其它部分）。", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
