import asyncio
import base64
import getpass
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

DB = os.getenv("DB_PATH", "relay.sqlite3")
CSRF = secrets.token_urlsafe(32)
TOKEN = re.compile(r"[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,100}")
CHAT = re.compile(r"(?:-100[0-9]{5,13}|@[A-Za-z0-9_]{5,32})")
STATE = {"status": "已停止", "identity": ""}
PROGRESS = {"processed": 0, "total": None, "sent": 0}
LOCK = threading.Lock()
STOP = threading.Event()
WORKER = None
LOGIN_LOCK = threading.Lock()
LOGIN_FLOW = None


@contextmanager
def database():
    conn = sqlite3.connect(DB)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    with database() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT);
            CREATE TABLE IF NOT EXISTS cursors (bot INTEGER PRIMARY KEY, offset INTEGER);
            CREATE TABLE IF NOT EXISTS pending (
                bot INTEGER, source INTEGER, album TEXT, message INTEGER, ready REAL,
                PRIMARY KEY (bot, source, message)
            );
            CREATE TABLE IF NOT EXISTS delivered (
                bot INTEGER, source INTEGER, album TEXT, target INTEGER,
                PRIMARY KEY (bot, source, album, target)
            );
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, backend TEXT NOT NULL, credential TEXT NOT NULL
            );
        """)
        row = conn.execute("SELECT data FROM config WHERE id=1").fetchone()
        if row:
            saved = json.loads(row[0])
            if "profile" not in saved:
                token = saved.pop("token", "")
                if token and TOKEN.fullmatch(token):
                    cursor = conn.execute("INSERT INTO profiles(name, backend, credential) VALUES (?, 'bot', ?)",
                                          ("原有 Bot", token))
                    if saved.get("backend") != "account":
                        saved["profile"] = str(cursor.lastrowid)
                saved.setdefault("profile", "legacy" if saved.get("backend") == "account" else "")
                conn.execute("UPDATE config SET data=? WHERE id=1", (json.dumps(saved),))


def log(detail):
    with database() as conn:
        conn.execute("INSERT INTO events(at, detail) VALUES(datetime('now'), ?)", (detail,))
        conn.execute("DELETE FROM events WHERE id <= (SELECT max(id)-300 FROM events)")


def account_file():
    directory = os.getenv("CREDENTIALS_DIRECTORY", "/etc/telegram-media-relay")
    return Path(directory) / "telegram-account.json"


def account_login():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    async def login():
        api_id = int(input("Telegram API ID: ").strip())
        api_hash = getpass.getpass("Telegram API Hash: ").strip()
        if api_id <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ValueError("API ID 或 API Hash 无效")
        client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await client.start(code_callback=lambda: getpass.getpass("Telegram 登录验证码: "))
            user = await client.get_me()
            if user.bot:
                raise ValueError("请输入 Telegram 个人账号手机号")
            path = account_file()
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"id": api_id, "hash": api_hash, "session": client.session.save()}))
            os.replace(temp, path)
            print("账号已登录，请重启转发服务。")
        finally:
            await client.disconnect()

    asyncio.run(login())


async def account_run(settings, action, start, end):
    from telethon import TelegramClient, events, errors
    from telethon.sessions import StringSession
    from telethon.tl.types import Channel

    credential = settings.get("account") or account_file().read_text()
    if not credential:
        raise ValueError("账号尚未登录，请按部署说明在服务器终端登录。")
    account = json.loads(credential)
    client = TelegramClient(StringSession(account["session"]), account["id"], account["hash"],
                            flood_sleep_threshold=0, request_retries=1)
    await client.connect()
    try:
        user = await client.get_me()
        if not user:
            raise ValueError("账号会话已失效，请重新登录。")
        STATE["identity"] = "@" + user.username if user.username else str(user.id)
        # Load dialogs so numeric IDs can resolve private channels and their access hashes.
        await client.get_dialogs()
        source_id = settings["source"]
        source = await client.get_entity(int(source_id) if source_id.startswith("-") else source_id)
        targets = {}
        for item in settings["targets"]:
            entity = await client.get_entity(int(item) if item.startswith("-") else item)
            if not isinstance(entity, Channel) or not entity.broadcast or entity.id == source.id:
                raise ValueError("目标必须是其他频道。")
            if not entity.creator and not getattr(entity.admin_rights, "post_messages", False):
                raise ValueError(f"目标 {item} 未授予账号发帖权限。")
            targets[entity.id] = entity
        if not isinstance(source, Channel) or not source.broadcast:
            raise ValueError("来源必须是频道。")
        if action == "test":
            log(f"账号连接验证通过 · {len(targets)} 个目标")
            return
        sending = asyncio.Lock()

        async def deliver(messages):
            async with sending:
                if STOP.is_set():
                    return
                allowed = [msg for msg in messages if not msg.noforwards and not msg.action]
                if source.noforwards:
                    allowed = []
                if len(allowed) != len(messages):
                    log(f"跳过 {len(messages)-len(allowed)} 条受保护或服务消息")
                for target in targets.values():
                    if not allowed:
                        break
                    while not STOP.is_set():
                        try:
                            copy = action == "history" or settings["mode"] == "copy"
                            result = await client.forward_messages(
                                target, allowed, from_peer=source, drop_author=copy,
                                drop_media_captions=copy and settings.get("remove_caption", False))
                            count = sum(item is not None for item in result)
                            PROGRESS["sent"] += count
                            log(f"目标 {target.id} 已发送 {count}/{len(allowed)} 条")
                            break
                        except errors.FloodWaitError as exc:
                            log(f"目标 {target.id} 限流，等待 {exc.seconds} 秒")
                            for _ in range(exc.seconds):
                                if STOP.is_set():
                                    break
                                await asyncio.sleep(1)
                        except errors.RPCError as exc:
                            log(f"目标 {target.id} 发送失败 · {type(exc).__name__}")
                            break
                if end is None:
                    PROGRESS["processed"] += len(messages)

        if action == "live":
            async def album(event):
                await deliver(sorted(event.messages, key=lambda msg: msg.id))

            async def single(event):
                if not event.message.grouped_id:
                    await deliver([event.message])

            client.add_event_handler(album, events.Album(chats=source))
            client.add_event_handler(single, events.NewMessage(chats=source))
            STATE["status"] = "账号实时转发中"
            log("账号实时监听已连接")
            while not STOP.is_set() and client.is_connected():
                await asyncio.sleep(0.5)
            if not STOP.is_set():
                raise ValueError("账号连接已断开，请重新启动实时任务。")
        else:
            STATE["status"] = "账号历史迁移中"
            group = []
            async for message in client.iter_messages(source, reverse=True, min_id=start-1,
                                                      max_id=end+1 if end else 0):
                if STOP.is_set():
                    break
                if group and (not message.grouped_id or group[0].grouped_id != message.grouped_id):
                    await deliver(group)
                    group = []
                group.append(message)
                if not message.grouped_id:
                    await deliver(group)
                    group = []
                if end:
                    PROGRESS["processed"] = message.id - start + 1
            if group and not STOP.is_set():
                await deliver(group)
            if end and not STOP.is_set():
                PROGRESS["processed"] = end - start + 1
    finally:
        await client.disconnect()


def config():
    with database() as conn:
        row = conn.execute("SELECT data FROM config WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {}


class TelegramError(Exception):
    def __init__(self, method, code=0, delay=0):
        super().__init__(f"{method} · {'HTTP ' + str(code) if code else '连接失败'}")
        self.code = code
        self.delay = delay


def api(token, method, **params):
    req = Request(f"https://api.telegram.org/bot{token}/{method}",
                  data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=18 if method == "getUpdates" else 12) as response:
            data = json.load(response)
        if data.get("ok"):
            return data["result"]
    except HTTPError as exc:
        delay = 0
        if exc.code == 429:
            try:
                delay = int(json.load(exc).get("parameters", {}).get("retry_after", 5))
            except (ValueError, OSError, TypeError):
                delay = 5
        raise TelegramError(method, exc.code, delay) from None
    except (URLError, OSError, ValueError):
        raise TelegramError(method) from None
    raise TelegramError(method)


def inspect(settings, verify_webhook=False):
    token = settings["token"]
    identity = api(token, "getMe")
    if verify_webhook and api(token, "getWebhookInfo").get("url"):
        raise ValueError("该 Bot 正在使用 Webhook，请先停止原有服务。")
    source = api(token, "getChat", chat_id=settings["source"])
    if source["type"] != "channel":
        raise ValueError("来源必须是频道。")
    membership = api(token, "getChatMember", chat_id=source["id"], user_id=identity["id"])
    if membership["status"] not in {"administrator", "creator", "member"}:
        raise ValueError("请先把 Bot 加入来源频道。")
    targets = []
    for name in settings["targets"]:
        target = api(token, "getChat", chat_id=name)
        if target["type"] != "channel" or target["id"] == source["id"]:
            raise ValueError("目标必须是其他频道。")
        membership = api(token, "getChatMember", chat_id=target["id"], user_id=identity["id"])
        if membership["status"] != "creator" and not (
            membership["status"] == "administrator" and membership.get("can_post_messages")
        ):
            raise ValueError(f"目标频道 {name} 未授予 Bot 发帖权限。")
        if target["id"] not in targets:
            targets.append(target["id"])
    return identity, source["id"], targets


def send(settings, source, target, ids):
    method = "forwardMessages" if settings["mode"] == "forward" else "copyMessages"
    options = {"remove_caption": True} if method == "copyMessages" and settings.get("remove_caption") else {}
    while not STOP.is_set():
        try:
            return len(api(settings["token"], method, from_chat_id=source, chat_id=target,
                           message_ids=ids, **options))
        except TelegramError as exc:
            if exc.code != 429:
                raise
            log(f"目标 {target} 触发限流，等待 {exc.delay or 5} 秒")
            STOP.wait(max(1, exc.delay or 5))
    return None


def ingest(bot, source, updates):
    now = time.time()
    with database() as conn:
        for update in updates:
            post = update.get("channel_post")
            if post and post["chat"]["id"] == source:
                message = post["message_id"]
                album = str(post.get("media_group_id") or f"single:{message}")
                ready = now + 2.5 if post.get("media_group_id") else now
                conn.execute("UPDATE pending SET ready=? WHERE bot=? AND source=? AND album=?",
                             (ready, bot, source, album))
                conn.execute("INSERT OR IGNORE INTO pending VALUES (?, ?, ?, ?, ?)",
                             (bot, source, album, message, ready))
        if updates:
            conn.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)",
                         (bot, updates[-1]["update_id"] + 1))


def flush(settings, bot, source, targets):
    with database() as conn:
        batches = conn.execute("""
            SELECT album FROM pending WHERE bot=? AND source=?
            GROUP BY album HAVING max(ready)<=? ORDER BY min(message) LIMIT 30
        """, (bot, source, time.time())).fetchall()
    for (album,) in batches:
        failed = False
        with database() as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT message FROM pending WHERE bot=? AND source=? AND album=? ORDER BY message",
                (bot, source, album))]
            done = {row[0] for row in conn.execute(
                "SELECT target FROM delivered WHERE bot=? AND source=? AND album=?",
                (bot, source, album))}
        for target in targets:
            if STOP.is_set():
                return
            if target in done:
                continue
            try:
                count = send(settings, source, target, ids)
            except TelegramError as exc:
                log(f"目标 {target} 转发失败，待重试 · {exc}")
                failed = True
                continue
            if count is None:
                return
            with database() as conn:
                conn.execute("INSERT OR IGNORE INTO delivered VALUES (?, ?, ?, ?)",
                             (bot, source, album, target))
            log(f"目标 {target} 已发送 {count}/{len(ids)} 条")
            PROGRESS["sent"] += count
        if failed:
            with database() as conn:
                conn.execute("UPDATE pending SET ready=? WHERE bot=? AND source=? AND album=?",
                             (time.time() + 30, bot, source, album))
            continue
        with database() as conn:
            conn.execute("DELETE FROM pending WHERE bot=? AND source=? AND album=?", (bot, source, album))
            conn.execute("DELETE FROM delivered WHERE bot=? AND source=? AND album=?", (bot, source, album))
        PROGRESS["processed"] += len(ids)


def live(settings, bot, source, targets):
    last_error = ""
    while not STOP.is_set():
        flush(settings, bot, source, targets)
        with database() as conn:
            cursor = conn.execute("SELECT offset FROM cursors WHERE bot=?", (bot,)).fetchone()
            waiting = conn.execute("SELECT 1 FROM pending WHERE bot=? AND source=? LIMIT 1",
                                   (bot, source)).fetchone()
        try:
            updates = api(settings["token"], "getUpdates", offset=cursor[0] if cursor else 0,
                          timeout=1 if waiting else 10, allowed_updates=["channel_post"])
            ingest(bot, source, updates)
            if last_error:
                log("Telegram 轮询已恢复")
            last_error = ""
            STATE["status"] = "实时转发中"
        except TelegramError as exc:
            STATE["status"] = f"轮询重试 · {exc}"
            if str(exc) != last_error:
                log(STATE["status"])
                last_error = str(exc)
            STOP.wait(max(5, exc.delay))


def migrate(settings, source, targets, start, end):
    settings = {**settings, "mode": "copy"}
    copied = 0
    empty = 0
    for first in range(start, (end or 2147483647) + 1, 100):
        last = min(first + 99, end or 2147483647)
        ids = list(range(first, last + 1))
        delivered = 0
        for target in targets:
            count = send(settings, source, target, ids)
            if count is None:
                return
            delivered += count
            log(f"历史 {first}–{last} → {target} · 复制 {count} 条，跳过 {len(ids)-count} 条")
        copied += delivered
        PROGRESS.update(processed=last-start+1, sent=copied)
        STATE["status"] = f"历史迁移已扫描至 {last} · 共复制 {copied} 条"
        empty = 0 if delivered else empty + 1
        if end is None and empty >= 3:
            log("连续 300 个 ID 无可复制消息，已停止自动扫描。")
            break
    if not STOP.is_set():
        log(f"历史迁移完成 · 共复制 {copied} 条")


def run(settings, action, start, end):
    try:
        if settings.get("backend") == "account":
            asyncio.run(account_run(settings, action, start, end))
        else:
            identity, source, targets = inspect(settings, action == "live")
            STATE["identity"] = "@" + identity["username"]
            if action == "live":
                log("实时转发已启动")
                live(settings, identity["id"], source, targets)
            else:
                log(f"历史迁移已启动 · 从 {start} 开始")
                migrate(settings, source, targets, start, end)
        STATE["status"] = "已停止" if STOP.is_set() else "已完成"
    except (TelegramError, ValueError) as exc:
        STATE["status"] = "任务失败"
        log(str(exc))
    except Exception as exc:
        STATE["status"] = "任务失败"
        log(f"任务异常 · {type(exc).__name__}")


def profile_choices():
    with database() as conn:
        return conn.execute("SELECT id, name, backend FROM profiles ORDER BY id").fetchall()


def add_profile(name, backend, credential):
    with database() as conn:
        conn.execute("INSERT INTO profiles(name, backend, credential) VALUES (?, ?, ?)",
                     (name, backend, credential))


async def telegram_login(action, fields):
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    global LOGIN_FLOW
    if action == "request":
        name = fields.get("name", "").strip()
        raw_id = fields.get("api_id", "").strip()
        api_hash = fields.get("api_hash", "").strip()
        phone = fields.get("phone", "").strip()
        if not 1 <= len(name) <= 64 or not re.fullmatch(r"[1-9][0-9]{0,9}", raw_id) or int(raw_id) > 2147483647:
            raise ValueError("请填写备注和有效的 API ID。")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) or not re.fullmatch(r"\+[1-9][0-9]{5,14}", phone):
            raise ValueError("请填写有效的 API Hash 和带国家区号的手机号。")
        flow = {"name": name, "id": int(raw_id), "hash": api_hash, "phone": phone,
                "session": "", "expires": time.monotonic() + 300}
    else:
        flow = LOGIN_FLOW
        if not flow or flow["expires"] < time.monotonic():
            LOGIN_FLOW = None
            raise ValueError("登录请求已过期，请重新发送验证码。")
        if action != flow["step"]:
            raise ValueError("请完成当前登录步骤。")
    client = TelegramClient(StringSession(flow["session"]), flow["id"], flow["hash"],
                            timeout=8, connection_retries=0, request_retries=0, flood_sleep_threshold=0)
    try:
        await client.connect()
        if action == "request":
            code = await client.send_code_request(flow["phone"])
            flow.update(code_hash=code.phone_code_hash, session=client.session.save(), step="verify")
            LOGIN_FLOW = flow
            return "已请求验证码，请查看 Telegram 登录消息。"
        if action == "verify":
            code = fields.get("code", "").strip()
            if not re.fullmatch(r"[0-9]{4,10}", code):
                raise ValueError("请输入数字验证码。")
            try:
                user = await client.sign_in(phone=flow["phone"], code=code, phone_code_hash=flow["code_hash"])
            except errors.SessionPasswordNeededError:
                flow.update(session=client.session.save(), step="password")
                return "该账号启用了两步验证，请输入两步验证密码。"
        else:
            password = fields.get("password", "")
            if not password:
                raise ValueError("请输入两步验证密码。")
            user = await client.sign_in(password=password)
        if not user or user.bot:
            raise ValueError("请使用 Telegram 个人账号。")
        credential = json.dumps({"id": flow["id"], "hash": flow["hash"], "session": client.session.save()})
        add_profile(flow["name"], "account", credential)
        LOGIN_FLOW = None
        return "账号已保存，可在转发设置中选择。"
    finally:
        await client.disconnect()


def manage_profile(fields):
    global LOGIN_FLOW
    action = fields.get("action", "")
    if action == "delete":
        selected = fields.get("profile", "")
        if not re.fullmatch(r"[1-9][0-9]{0,17}", selected):
            raise ValueError("请选择要删除的连接。")
        with LOCK:
            if WORKER and WORKER.is_alive():
                raise ValueError("请先停止当前任务并等待退出。")
            with database() as conn:
                cursor = conn.execute("DELETE FROM profiles WHERE id=?", (int(selected),))
                if not cursor.rowcount:
                    raise ValueError("该连接已删除或不存在。")
                conn.execute("""UPDATE config SET data=json_set(data, '$.profile', '', '$.auto_live', 0)
                                WHERE id=1 AND json_extract(data, '$.profile')=?""", (selected,))
        return "已删除连接。"
    if action == "add_bot":
        name = fields.get("name", "").strip()
        token = fields.get("token", "").strip()
        if not 1 <= len(name) <= 64 or not TOKEN.fullmatch(token):
            raise ValueError("请填写备注和有效的 Bot Token。")
        identity = api(token, "getMe")
        add_profile(name, "bot", token)
        return f"Bot @{identity['username']} 已保存。"
    with LOGIN_LOCK:
        if action == "cancel":
            LOGIN_FLOW = None
            return "已取消登录。"
        if action not in {"request", "verify", "password"}:
            raise ValueError("无法识别的操作。")
        try:
            return asyncio.run(telegram_login(action, fields))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Telegram 登录失败 · {type(exc).__name__}") from None


def attach_profile(settings):
    selected = settings.get("profile", "")
    if selected == "legacy" and account_file().is_file() and account_file().stat().st_size:
        return {**settings, "backend": "account"}
    if not re.fullmatch(r"[1-9][0-9]{0,17}", selected):
        raise ValueError("请先在账号管理添加账号或 Bot，再选择连接。")
    with database() as conn:
        row = conn.execute("SELECT backend, credential FROM profiles WHERE id=?", (int(selected),)).fetchone()
    if not row:
        raise ValueError("找不到所选连接。")
    backend, credential = row
    return {**settings, "backend": backend, ("token" if backend == "bot" else "account"): credential}


def fields_to_settings(fields):
    source = fields.get("source", "").strip()
    targets = list(dict.fromkeys(re.split(r"[,，\s]+", fields.get("targets", "").strip())))
    mode = fields.get("mode", "")
    if not CHAT.fullmatch(source) or not 1 <= len(targets) <= 20 or any(not CHAT.fullmatch(x) for x in targets):
        raise ValueError("请填写频道 @用户名或 -100 开头的数字 ID，最多 20 个目标。")
    if mode not in {"copy", "forward"}:
        raise ValueError("请选择转发方式。")
    return attach_profile({"profile": fields.get("profile", ""), "source": source, "targets": targets, "mode": mode,
            "remove_caption": fields.get("remove_caption") == "on"})


def control(fields):
    global WORKER
    action = fields.get("action")
    with LOCK:
        if action == "stop":
            STOP.set()
            with database() as conn:
                conn.execute("UPDATE config SET data=json_set(data, '$.auto_live', 0) WHERE id=1")
            if WORKER and WORKER.is_alive():
                STATE["status"] = "正在停止"
            return "已提交停止请求。"
        if WORKER and WORKER.is_alive():
            raise ValueError("请先停止当前任务并等待退出。")
        settings = fields_to_settings(fields)
        if action == "test":
            if settings["backend"] == "account":
                STOP.clear()
                STATE["status"] = "正在检查账号权限"
                WORKER = threading.Thread(target=run, args=(settings, action, None, None), daemon=True)
                WORKER.start()
                return "账号测试已提交，请查看运行日志。"
            identity, _, targets = inspect(settings)
            STATE["identity"] = "@" + identity["username"]
            return f"连接验证通过 · {STATE['identity']} · {len(targets)} 个目标频道"
        if action not in {"save", "live", "history"}:
            raise ValueError("无法识别的操作。")
        start = end = None
        if action == "history":
            raw_start = fields.get("start", "").strip()
            raw_end = fields.get("end", "").strip()
            if not re.fullmatch(r"[0-9]{1,10}", raw_start) or (
                raw_end and not re.fullmatch(r"[0-9]{1,10}", raw_end)
            ):
                raise ValueError("请输入数字消息 ID。")
            start = int(raw_start)
            end = int(raw_end) if raw_end else None
            if not 1 <= start <= 2147483647 or (end is not None and not start <= end <= 2147483647):
                raise ValueError("请输入有效的起止消息 ID。")
        settings["auto_live"] = action == "live"
        with database() as conn:
            saved = {key: value for key, value in settings.items() if key not in {"token", "account"}}
            conn.execute("INSERT OR REPLACE INTO config VALUES (1, ?)", (json.dumps(saved),))
        if action == "save":
            return "已保存配置。"
        STOP.clear()
        PROGRESS.update(processed=0, sent=0, total=end-start+1 if end is not None else None)
        STATE["status"] = "正在检查频道权限"
        WORKER = threading.Thread(target=run, args=(settings, action, start, end), daemon=True)
        WORKER.start()
        return "任务已提交。刷新可查看运行状态。"


class Admin(BaseHTTPRequestHandler):
    def authorized(self):
        if hmac.compare_digest(self.headers.get("Authorization", "").encode(), self.server.auth):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Telegram Media Relay"')
        self.end_headers()
        return False

    def page(self, notice="", fields=None, accounts=False):
        nonce = secrets.token_urlsafe(18)
        settings = config()
        fields = fields or {}
        value = lambda key, default="": html.escape(fields.get(key, default), quote=True)
        with database() as conn:
            events = conn.execute("SELECT at, detail FROM events ORDER BY id DESC LIMIT 100").fetchall()
        entries = "".join(f"<li><time>{at} UTC</time> {html.escape(detail)}</li>" for at, detail in events)
        mode = fields.get("mode", settings.get("mode", "copy"))
        selected = fields.get("profile", settings.get("profile", ""))
        choices = [(str(i), name, backend) for i, name, backend in profile_choices()]
        legacy = account_file()
        if legacy.is_file() and legacy.stat().st_size:
            choices.append(("legacy", "原有终端账号", "account"))
        options = "".join(
            f'<option value="{i}" {"selected" if i == selected else ""}>{html.escape(name)} · {"Bot" if backend == "bot" else "账号"}</option>'
            for i, name, backend in choices
        )
        profile_list = "".join(
            f'<li>{html.escape(name)} · {"Bot" if backend == "bot" else "Telegram 账号"}'
            + (f'<form class="delete-profile" method="post" action="/accounts">'
               f'<input type="hidden" name="csrf" value="{CSRF}">'
               f'<input type="hidden" name="profile" value="{i}">'
               '<button class="secondary" name="action" value="delete">删除</button></form>'
               if i != "legacy" else ' · 终端导入')
            + '</li>'
            for i, name, backend in choices
        )
        flow = LOGIN_FLOW
        step = flow["step"] if flow and flow["expires"] > time.monotonic() else ""
        login_form = {
            "verify": '<label>Telegram 验证码<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label><button name="action" value="verify">验证并保存</button>',
            "password": '<label>两步验证密码<input type="password" name="password" autocomplete="new-password" required></label><button name="action" value="password">登录并保存</button>',
        }.get(step, """<label>备注<input name="name" maxlength="64" required></label>
        <label>API ID<input name="api_id" inputmode="numeric" required></label>
        <label>API Hash<input name="api_hash" type="password" autocomplete="new-password" required></label>
        <label>手机号<input name="phone" type="tel" placeholder="+852…" required></label>
        <button name="action" value="request">发送验证码</button>""")
        remove_caption = fields.get("remove_caption") == "on" if fields else settings.get("remove_caption", False)
        body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>频道媒体组转发</title>
        <style>
        *{{box-sizing:border-box}}[hidden]{{display:none!important}}body{{margin:0;background:#f0f4f7;color:#12273b;font:15px Inter,system-ui,sans-serif}}
        .layout{{display:grid;grid-template-columns:210px minmax(0,1fr);min-height:100vh}}
        aside{{background:#142d44;color:#fff;padding:24px 16px}}
        aside strong{{display:block;margin:0 8px 20px;font-size:18px}}
        aside a{{display:block;color:#e4efff;padding:10px 12px;text-decoration:none;border-radius:8px}}
        aside a:hover{{background:#254866}}
        header{{padding:24px 24px 0;max-width:1080px;margin:auto}}
        h1{{margin:0 0 7px;font-size:26px}}h2{{font-size:18px;margin:0 0 12px}}
        main{{max-width:1080px;margin:auto;padding:24px;display:grid;grid-template-columns:1.1fr 1fr;gap:18px}}
        section{{background:white;border:1px solid #dce6ef;border-radius:14px;padding:20px;min-width:0}}
        label{{display:block;margin:12px 0;color:#304961}}input:not([type=checkbox]),select{{width:100%;padding:10px;margin-top:5px;border:1px solid #a5b7c8;border-radius:7px;font:inherit}}
        .row,.actions{{display:flex;gap:8px;flex-wrap:wrap}}.row label{{flex:1}}button,a{{font:inherit}}
        button{{cursor:pointer;background:#155c9e;color:white;border:0;border-radius:7px;padding:8px 12px}}
        .secondary{{background:#e6eef6;color:#193d61}}ol{{padding-left:22px;max-height:580px;overflow:auto}}
        .delete-profile{{display:inline-block;margin-left:12px}}
        li{{padding:9px 0;border-bottom:1px solid #e5edf3;overflow-wrap:anywhere}}time{{color:#52667b;display:block;font-size:12px}}
        .notice{{grid-column:1/-1;margin:0;padding:10px;background:#dcecff;border-radius:7px}}
        progress{{width:100%;height:12px}} #progress-label{{color:#304961}}
        @media(max-width:920px){{main{{grid-template-columns:1fr}}}}
        @media(max-width:640px){{.layout{{display:block}}aside nav{{display:flex;flex-wrap:wrap}}}}
        </style><div class="layout"><aside><strong>媒体组转发</strong><nav>
        <a href="/#settings">转发设置</a><a href="/accounts">账号管理</a><a href="/#history">历史迁移</a><a href="/#runtime">运行进度与日志</a>
        </nav></aside><div><header><h1>频道媒体组转发</h1><span id="status">{html.escape(STATE['status'])}</span>
        · <span id="identity">{html.escape(STATE['identity'] or '未验证')}</span></header>
        <main>{f'<p class="notice" role="status">{html.escape(notice)}</p>' if notice else ''}
        <section id="settings" {'hidden' if accounts else ''}><h2>转发设置</h2><form method="post" action="/">
        <input type="hidden" name="csrf" value="{CSRF}">
        <label>使用账号或 Bot<select name="profile"><option value="">请选择已配置的连接</option>{options}</select></label>
        <a href="/accounts">添加账号或 Bot</a>
        <label>来源频道<input name="source" value="{value('source', settings.get('source',''))}" placeholder="@source 或 -100…"></label>
        <label>目标频道<input name="targets" value="{value('targets', ', '.join(settings.get('targets', [])))}" placeholder="多个频道用逗号分隔"></label>
        <label>实时转发方式<select name="mode"><option value="copy" {'selected' if mode=='copy' else ''}>无引用复制</option>
        <option value="forward" {'selected' if mode=='forward' else ''}>带引用转发</option></select></label>
        <label><input type="checkbox" name="remove_caption" {'checked' if remove_caption else ''}> 复制时清除媒体说明</label>
        <div class="actions"><button name="action" value="save" class="secondary">保存配置</button>
        <button name="action" value="test" class="secondary">测试连接</button>
        <button name="action" value="live">开始实时转发</button><button name="action" value="stop" class="secondary">停止任务</button></div>
        <h2 id="history" style="margin-top:24px">历史迁移</h2><div class="row">
        <label>起始消息 ID<input name="start" inputmode="numeric" value="{value('start')}"></label>
        <label>结束消息 ID<input name="end" inputmode="numeric" value="{value('end')}" placeholder="留空自动扫描"></label></div>
        <button name="action" value="history">开始历史复制</button></form></section>
        <section id="runtime" {'hidden' if accounts else ''}><h2>运行进度与日志</h2>
        <p id="progress-label" role="status">已处理 0 条 · 已发送 0 条</p>
        <progress id="progress" aria-label="历史扫描进度" hidden></progress>
        <ol id="logs">{entries or '<li>暂无运行记录</li>'}</ol></section>
        <section {'hidden' if not accounts else ''}><h2>Telegram 账号登录</h2>
        <p>使用 HTTPS 或 SSH 隧道访问此页后再输入登录凭据。</p>
        <form method="post" action="/accounts"><input type="hidden" name="csrf" value="{CSRF}">
        {login_form}<button class="secondary" name="action" value="cancel" formnovalidate>取消</button></form></section>
        <section {'hidden' if not accounts else ''}><h2>添加 Bot</h2>
        <form method="post" action="/accounts"><input type="hidden" name="csrf" value="{CSRF}">
        <label>备注<input name="name" maxlength="64" required></label>
        <label>Bot Token<input name="token" type="password" autocomplete="new-password" required></label>
        <button name="action" value="add_bot">验证并保存</button></form>
        <h2 style="margin-top:24px">已配置的连接</h2><ul>{profile_list or '<li>暂无</li>'}</ul></section>
        </main></div></div><script nonce="{nonce}">
        const get = id => document.getElementById(id);
        for (const form of document.querySelectorAll(".delete-profile")) {{
            form.addEventListener("submit", event => {{
                if (!confirm("确定删除此连接？")) event.preventDefault();
            }});
        }}
        let newest = null;
        async function refresh() {{
            try {{
                const response = await fetch("/status", {{cache:"no-store"}});
                if (!response.ok) return;
                const data = await response.json();
                get("status").textContent = data.status;
                get("identity").textContent = data.identity || "未验证";
                const total = data.total ? "/" + data.total : "";
                get("progress-label").textContent = "已处理 " + data.processed + total + " 条 · 已发送 " + data.sent + " 条";
                const bar = get("progress");
                bar.hidden = !data.total;
                if (data.total) {{ bar.max = data.total; bar.value = data.processed; }}
                const head = data.logs[0]?.[0];
                if (head === newest || head === undefined) return;
                newest = head;
                get("logs").replaceChildren(...data.logs.map(([, at, detail]) => {{
                    const item = document.createElement("li");
                    const time = document.createElement("time");
                    time.textContent = at + " UTC";
                    item.append(time, detail);
                    return item;
                }}));
            }} catch {{
                get("status").textContent = "无法刷新状态，正在重试";
            }} finally {{
                setTimeout(refresh, 1500);
            }}
        }}
        refresh();
        </script></html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.authorized():
            if self.path == "/":
                self.page()
            elif self.path == "/accounts":
                self.page(accounts=True)
            elif self.path == "/status":
                with database() as conn:
                    logs = conn.execute("SELECT id, at, detail FROM events ORDER BY id DESC LIMIT 100").fetchall()
                body = json.dumps({**STATE, **PROGRESS, "logs": logs}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

    def do_POST(self):
        if not self.authorized():
            return
        if self.path not in {"/", "/accounts"}:
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 8192:
                raise ValueError()
            fields = {key: values[0] for key, values in parse_qs(
                self.rfile.read(size).decode(), max_num_fields=10, keep_blank_values=True).items()}
        except ValueError:
            self.send_error(400)
            return
        if not hmac.compare_digest(fields.get("csrf", "").encode(), CSRF.encode()):
            self.send_error(403)
            return
        try:
            notice = manage_profile(fields) if self.path == "/accounts" else control(fields)
        except (ValueError, TelegramError) as exc:
            notice = str(exc)
        self.page(notice, fields if self.path == "/" else None, accounts=self.path == "/accounts")

    def setup(self):
        super().setup()
        self.connection.settimeout(10)


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:] == ["--login"]:
        account_login()
        sys.exit(0)
    init()
    credentials = f"{os.environ['ADMIN_USERNAME']}:{os.environ['ADMIN_PASSWORD']}"
    server = ThreadingHTTPServer((os.getenv("BIND_HOST", "127.0.0.1"), int(os.getenv("PORT", "8766"))), Admin)
    server.auth = b"Basic " + base64.b64encode(credentials.encode())
    saved = config()
    if saved.get("auto_live"):
        try:
            saved = attach_profile(saved)
        except ValueError as exc:
            STATE["status"] = str(exc)
        else:
            STATE["status"] = "正在恢复实时转发"
            WORKER = threading.Thread(target=run, args=(saved, "live", None, None), daemon=True)
            WORKER.start()
    server.serve_forever()

