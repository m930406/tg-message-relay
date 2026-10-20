"""Matrix <-> Telegram relay.

把本機 Matrix 帳號收到的每個聊天室（由 FB / IG / LINE / Discord bridge 建立）對應到
Telegram 論壇群組裡的一個「主題」：
  - Matrix 新訊息  -> 轉到對應主題（沒有就自動建立）
  - 你在主題裡回覆 -> 以你的 Matrix 帳號送回該聊天室，再由 bridge 送回 FB / IG / LINE / Discord
"""

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import aiohttp

log = logging.getLogger("relay")

PLATFORM_LABEL = {"fb": "FB", "ig": "IG", "line": "LINE", "discord": "Discord"}
PLATFORM_ICON = {"fb": "🔵", "ig": "🟣", "line": "🟢", "discord": "👾"}

HS_URL = os.environ.get("HS_URL", "http://synapse:8008").rstrip("/")
SERVER_NAME = os.environ.get("SERVER_NAME", "home.local")
MATRIX_USER = os.environ["MATRIX_USER"]
MATRIX_PASSWORD = os.environ["MATRIX_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"]) if os.environ.get("TELEGRAM_CHAT_ID") else None
TG_OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"]) if os.environ.get("TELEGRAM_OWNER_ID") else None
# 啟用的平台跟 docker compose 共用同一個設定（.env 的 COMPOSE_PROFILES）
BRIDGES = [b for b in re.split(r"[\s,]+", os.environ.get("COMPOSE_PROFILES", "fb,ig,line")) if b in PLATFORM_LABEL]
DB_PATH = os.environ.get("DB_PATH", "relay.db")
# 這些平台只轉發你加入白名單的聊天（在該平台的「🤖 登入 / 管理」主題用 /chats 加入）
APPROVE_PLATFORMS = [b for b in re.split(r"[\s,]+", os.environ.get("APPROVE_NEW_CHATS", "line")) if b in PLATFORM_LABEL]
# Discord 一律只轉發你挑的頻道（加入一個伺服器就會有一堆頻道）
APPROVE_PLATFORMS += [] if "discord" in APPROVE_PLATFORMS else ["discord"]
PENDING_KEEP = 20  # 等你決定期間，每個聊天最多先保留幾則訊息
# 這些平台的「訊息請求」先不轉發（IG：你沒追蹤的人傳來的私訊會被 IG 放進訊息請求）。
# 在 App 裡接受請求後才開始轉，接受前收到的最近幾則會補送。留空 = 訊息請求也照常轉發
HOLD_REQUEST_PLATFORMS = [b for b in re.split(r"[\s,]+", os.environ.get("HOLD_MESSAGE_REQUESTS", "ig"))
                          if b in PLATFORM_LABEL]
BRIDGE_INFO_TYPES = ("m.bridge", "uk.half-shot.bridge")
TG_API_URL = os.environ.get("TG_API_URL", "https://api.telegram.org").rstrip("/")
# bridge 的 provisioning API（docker compose 內的服務名稱與 port），用來查「你自己」在各平台的帳號
BRIDGE_URLS = {"fb": "http://fb:29319", "ig": "http://ig:29330", "line": "http://line:29322",
               "discord": "http://discord:29334"}
# Discord bridge 是舊版架構，provisioning API 用共用密碼驗證（setup.sh 產生，放在 relay 的資料夾）
DISCORD_SECRET_FILE = os.environ.get("DISCORD_SECRET_FILE", "/data/discord_provision_secret")
# 每天幾點（台灣時間）檢查各平台登入狀態並回報到 General；留空就不檢查
LOGIN_CHECK_TIME = os.environ.get("LOGIN_CHECK_TIME", "09:00").strip()
TAIWAN = timezone(timedelta(hours=8))
# 只有這段時間（台灣時間）轉發的訊息會跳通知，其他時間靜音送達；留空 = 隨時通知
NOTIFY_HOURS = os.environ.get("NOTIFY_HOURS", "").strip()   # 例如 08:00-22:00（也可以跨夜，例如 22:00-06:00）
NOTIFY_DAYS = os.environ.get("NOTIFY_DAYS", "").strip()     # 例如 1-5（週一到週五）或 1,3,5；留空 = 每天
# 靜音時段結束前幾分鐘就開始補送（靜音送達），通知時段一到訊息已經在主題裡；0 = 通知時段到了才補送
try:
    PRELOAD_MINUTES = max(0, int(os.environ.get("PRELOAD_MINUTES", "60").strip() or 0))
except ValueError:
    PRELOAD_MINUTES = 60
HOLIDAY_MAX_DAYS = 60  # /off 一次最多指定幾天
# 群組最多幾位成員（你和 bot = 2）。超過就暫停轉發，避免別人看到你的聊天
try:
    GROUP_MAX_MEMBERS = max(2, int(os.environ.get("GROUP_MAX_MEMBERS", "2").strip() or 2))
except ValueError:
    GROUP_MAX_MEMBERS = 2
MEMBER_CHECK_SECONDS = 60

MY_MXID = f"@{MATRIX_USER}:{SERVER_NAME}"
ANONYMOUS_ADMIN_ID = 1087968824  # 群組裡「保持匿名」發言時，Telegram 給的共用 ID（GroupAnonymousBot）
ANONYMOUS_HINT = "這是「匿名管理員」的共用 ID，不是你的帳號。請到群組設定 → 管理員 → 你自己，關掉「保持匿名」後再講一次話。"
TG_TEXT_LIMIT = 4000      # Telegram 上限 4096（UTF-16 長度），留一點餘裕
TG_CAPTION_LIMIT = 1000   # Telegram 上限 1024
TG_UPLOAD_LIMIT = 50 * 1024 * 1024
# bridge bot 在等你輸入密碼 / cookie 時，你在 Telegram 打的那則訊息轉送後會被自動刪除。
# 只有「要求輸入」的提示（Please enter your …、Enter a JSON object with your cookies …）會切換狀態，
# 其他說明訊息（例如 Login URL: …）不影響；登入結束 / 取消 / 失敗就解除。
PROMPT_RE = re.compile(r"^\s*(please\s+)?(enter|paste)\b", re.I)
SECRET_RE = re.compile(r"password|passcode|cookie|curl|token|2fa|\bcode\b|密碼|驗證碼", re.I)
LOGIN_END_RE = re.compile(r"login cancelled|logged in|login failed|no ongoing", re.I)
SYNC_FILTER = json.dumps({
    "presence": {"not_types": ["*"]},
    "account_data": {"not_types": ["*"]},
    "room": {
        "timeline": {"limit": 50},
        "state": {"lazy_load_members": True},
        "ephemeral": {"not_types": ["*"]},
        "account_data": {"not_types": ["*"]},
    },
})


def platform_of(mxid):
    """回傳 (平台代號, 是否為 bridge bot)，不是 bridge 的使用者回傳 (None, False)。"""
    local = mxid[1:].split(":", 1)[0]
    for b in BRIDGES:
        if local == f"{b}bot":
            return b, True
        if local.startswith(f"{b}_"):
            return b, False
    return None, False


def ghost_mxid(platform, remote_id):
    """bridge 把平台上的使用者 ID 編碼成 Matrix 帳號（同 mautrix 的 EncodeUserLocalpart）。"""
    out = []
    for b in remote_id.encode():
        c = chr(b)
        if c.isascii() and (c.islower() or c.isdigit() or c in "-.+"):
            out.append(c)
        elif "A" <= c <= "Z" or c == "_":
            out.append("_" + ("_" if c == "_" else c.lower()))
        else:
            out.append(f"={b:02x}")
    return f"@{platform}_{''.join(out)}:{SERVER_NAME}"


def _parse_days(spec):
    days = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        lo, _, hi = part.partition("-")
        days.update(range(int(lo), int(hi or lo) + 1))
    return days


def _parse_hours(spec):
    start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in spec.split("-"))
    return start, end


def notify_now(now=None):
    """現在是不是該跳通知的時間（通知時段）。設定有誤時當作隨時通知，並記錄錯誤。"""
    if not NOTIFY_HOURS and not NOTIFY_DAYS:
        return True
    now = now or datetime.now(TAIWAN)
    try:
        if NOTIFY_DAYS and now.isoweekday() not in _parse_days(NOTIFY_DAYS):
            return False
        if NOTIFY_HOURS:
            start, end = _parse_hours(NOTIFY_HOURS)
            t = now.time()
            return start <= t < end if start <= end else (t >= start or t < end)  # 跨夜時段
    except ValueError:
        log.error("NOTIFY_HOURS / NOTIFY_DAYS 格式錯誤（%r / %r），先當作隨時通知", NOTIFY_HOURS, NOTIFY_DAYS)
    return True


def bot_title(platform):
    return f"🤖 {PLATFORM_LABEL.get(platform, '?')} 登入 / 管理"


def esc(text):
    return html.escape(text, quote=False)


def tg_len(text):
    """Telegram 用 UTF-16 長度計算字數（emoji 算 2）。"""
    return len(text.encode("utf-16-le")) // 2


def html_chunks(text, limit=TG_TEXT_LIMIT):
    """把純文字做 HTML 跳脫後切成多段，每段不超過 limit，且不會把 &amp; 這類跳脫字元切斷。
    盡量在換行處切開。"""
    chunks, cur, size, last_nl = [], [], 0, -1
    for ch in text:
        piece = esc(ch)
        n = tg_len(piece)
        while cur and size + n > limit:
            cut = last_nl + 1 if last_nl >= 0 else len(cur)
            chunks.append("".join(cur[:cut]))
            cur = cur[cut:]
            size = sum(map(tg_len, cur))
            last_nl = -1
        if ch == "\n":
            last_nl = len(cur)
        cur.append(piece)
        size += n
    chunks.append("".join(cur))
    return chunks


def html_fit(text, limit):
    """HTML 跳脫並截斷到 limit 以內（截斷時補 …）。"""
    chunks = html_chunks(text, limit - 1)
    return chunks[0] + ("…" if len(chunks) > 1 else "")


def strip_reply_fallback(body):
    lines = body.split("\n")
    if not lines or not lines[0].startswith("> "):
        return body
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    if lines and lines[0] == "":
        lines.pop(0)
    return "\n".join(lines)


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_TAGS = {"br", "p", "div", "li", "ul", "ol", "blockquote", "pre", "tr", "table",
              "h1", "h2", "h3", "h4", "h5", "h6"}


class _VisibleText(HTMLParser):
    """formatted_body 轉純文字，略過 Discord 的連結預覽（<blockquote class="discord-embed">）和回覆引用（<mx-reply>）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if self.skip:
            self.skip += tag not in VOID_TAGS
        elif tag == "mx-reply" or (tag == "blockquote" and "discord-embed" in (dict(attrs).get("class") or "").split()):
            self.skip = 1
        elif tag in BLOCK_TAGS:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):  # <br/> 這種自己結束的標籤
        if not self.skip and tag in BLOCK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= tag not in VOID_TAGS
        elif tag in BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def text_fingerprint(content, is_reply=False):
    """訊息「看得到的文字」的指紋：拿掉連結預覽、回覆引用，空白統一。
    用來判斷 bridge 送來的「編輯」是不是真的改了字（Discord 更新連結預覽時也會送編輯）。只存雜湊，不存內容。"""
    if content.get("format") == "org.matrix.custom.html" and content.get("formatted_body"):
        parser = _VisibleText()
        parser.feed(content["formatted_body"])
        parser.close()
        text = "".join(parser.out)
    else:
        text = content.get("body", "")
        text = strip_reply_fallback(text) if is_reply else text
    text = " ".join(text.split())
    return hashlib.sha256(f"{content.get('msgtype')}\n{text}".encode()).hexdigest()[:32]


# ───────────────────────────── 資料庫 ─────────────────────────────

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS rooms (
                room_id TEXT PRIMARY KEY,
                topic_id INTEGER,
                platform TEXT,
                title TEXT,
                is_bot INTEGER DEFAULT 0,
                ignored INTEGER DEFAULT 0,
                approved INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS pending (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT,
                event TEXT
            );
            CREATE TABLE IF NOT EXISTS msgs (
                tg_msg_id INTEGER PRIMARY KEY,
                room_id TEXT,
                event_id TEXT
            );
            CREATE INDEX IF NOT EXISTS msgs_event ON msgs(event_id);
            CREATE TABLE IF NOT EXISTS shown (
                event_id TEXT PRIMARY KEY,
                fp TEXT
            );
            CREATE TABLE IF NOT EXISTS deferred (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT,
                event TEXT
            );
        """)
        cols = [c[1] for c in self.db.execute("PRAGMA table_info(rooms)")]
        if "approved" not in cols:  # 舊版資料庫：既有的聊天都算已接收
            self.db.execute("ALTER TABLE rooms ADD COLUMN approved INTEGER DEFAULT 1")
        self.db.commit()

    def add_deferred(self, room_id, ev):
        self.db.execute("INSERT INTO deferred (room_id, event) VALUES (?, ?)", (room_id, json.dumps(ev)))
        self.db.commit()

    def deferred_count(self):
        return self.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0]

    def next_deferred(self, bot_only=False):
        row = self.db.execute(
            "SELECT seq, room_id, event FROM deferred ORDER BY seq LIMIT 1" if not bot_only else
            "SELECT d.seq, d.room_id, d.event FROM deferred d JOIN rooms r ON r.room_id = d.room_id"
            " WHERE r.is_bot = 1 ORDER BY d.seq LIMIT 1").fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    def del_deferred(self, seq):
        self.db.execute("DELETE FROM deferred WHERE seq=?", (seq,))
        self.db.commit()

    def get(self, key):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self.db.commit()

    ROOM_COLS = ("room_id", "topic_id", "platform", "title", "is_bot", "ignored", "approved")

    def room(self, room_id):
        row = self.db.execute(f"SELECT {', '.join(self.ROOM_COLS)} FROM rooms WHERE room_id=?",
                              (room_id,)).fetchone()
        return dict(zip(self.ROOM_COLS, row)) if row else None

    def room_rowid(self, room_id):
        return self.db.execute("SELECT rowid FROM rooms WHERE room_id=?", (room_id,)).fetchone()[0]

    def room_by_rowid(self, rowid):
        row = self.db.execute("SELECT room_id FROM rooms WHERE rowid=?", (rowid,)).fetchone()
        return self.room(row[0]) if row else None

    def add_pending(self, room_id, ev):
        self.db.execute("INSERT INTO pending (room_id, event) VALUES (?, ?)", (room_id, json.dumps(ev)))
        self.db.execute("DELETE FROM pending WHERE room_id=? AND seq NOT IN "
                        "(SELECT seq FROM pending WHERE room_id=? ORDER BY seq DESC LIMIT ?)",
                        (room_id, room_id, PENDING_KEEP))
        self.db.commit()

    def pop_pending(self, room_id):
        rows = self.db.execute("SELECT event FROM pending WHERE room_id=? ORDER BY seq", (room_id,)).fetchall()
        self.db.execute("DELETE FROM pending WHERE room_id=?", (room_id,))
        self.db.commit()
        return [json.loads(r[0]) for r in rows]

    def room_by_topic(self, topic_id):
        row = self.db.execute("SELECT room_id FROM rooms WHERE topic_id=?", (topic_id,)).fetchone()
        return self.room(row[0]) if row else None

    def save_room(self, r):
        r.setdefault("approved", 1)
        self.db.execute(
            f"INSERT OR REPLACE INTO rooms ({', '.join(self.ROOM_COLS)}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(r[c] for c in self.ROOM_COLS))
        self.db.commit()

    def save_msg(self, tg_msg_id, room_id, event_id):
        self.db.execute("INSERT OR REPLACE INTO msgs VALUES (?, ?, ?)", (tg_msg_id, room_id, event_id))
        self.db.commit()

    def event_for_tg(self, tg_msg_id):
        row = self.db.execute("SELECT room_id, event_id FROM msgs WHERE tg_msg_id=?", (tg_msg_id,)).fetchone()
        return row if row else (None, None)

    def shown_fp(self, event_id):
        """這則訊息上次送進 Telegram 時的文字指紋（編輯過就是最新的版本）。"""
        row = self.db.execute("SELECT fp FROM shown WHERE event_id=?", (event_id,)).fetchone()
        return row[0] if row else None

    def set_shown_fp(self, event_id, fp):
        self.db.execute("INSERT OR REPLACE INTO shown VALUES (?, ?)", (event_id, fp))
        self.db.commit()

    def tg_for_event(self, event_id):
        row = self.db.execute(
            "SELECT tg_msg_id FROM msgs WHERE event_id=? ORDER BY tg_msg_id LIMIT 1", (event_id,)).fetchone()
        return row[0] if row else None


# ───────────────────────────── Matrix ─────────────────────────────

class MatrixError(Exception):
    def __init__(self, status, data):
        super().__init__(f"{status} {data}")
        self.status = status
        self.data = data


class Matrix:
    def __init__(self, session, store):
        self.http = session
        self.store = store
        self.token = None
        self.txn = int(time.time() * 1000)

    async def req(self, method, path, *, json_body=None, params=None, data=None, headers=None,
                  raw=False, timeout=60):
        hdrs = dict(headers or {})
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        async with self.http.request(method, HS_URL + path, json=json_body, params=params, data=data,
                                     headers=hdrs, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if raw:
                if r.status != 200:
                    raise MatrixError(r.status, await r.text())
                return await r.read(), r.headers.get("Content-Type", "application/octet-stream")
            body = await r.json(content_type=None)
            if r.status == 429:
                await asyncio.sleep(body.get("retry_after_ms", 1000) / 1000)
                return await self.req(method, path, json_body=json_body, params=params, data=data,
                                      headers=headers, raw=raw, timeout=timeout)
            if r.status >= 400:
                raise MatrixError(r.status, body)
            return body

    async def login(self):
        self.token = self.store.get("access_token")
        if self.token:
            try:
                await self.req("GET", "/_matrix/client/v3/account/whoami")
                return
            except MatrixError as e:
                if e.status != 401:
                    raise
        self.token = None
        resp = await self.req("POST", "/_matrix/client/v3/login", json_body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": MATRIX_USER},
            "password": MATRIX_PASSWORD,
            "initial_device_display_name": "telegram-relay",
        })
        self.token = resp["access_token"]
        self.store.set("access_token", self.token)

    @staticmethod
    def q(s):
        return urllib.parse.quote(s, safe="")

    async def send(self, room_id, content, etype="m.room.message"):
        self.txn += 1
        resp = await self.req("PUT", f"/_matrix/client/v3/rooms/{self.q(room_id)}/send/{etype}/{self.txn}",
                              json_body=content)
        return resp["event_id"]

    async def state(self, room_id, etype, key=""):
        try:
            return await self.req("GET", f"/_matrix/client/v3/rooms/{self.q(room_id)}/state/{etype}/{self.q(key)}")
        except MatrixError as e:
            if e.status == 404:
                return None
            raise

    async def full_state(self, room_id):
        return await self.req("GET", f"/_matrix/client/v3/rooms/{self.q(room_id)}/state")

    async def join(self, room_id):
        await self.req("POST", f"/_matrix/client/v3/join/{self.q(room_id)}", json_body={})

    async def receipt(self, room_id, event_id):
        await self.req("POST", f"/_matrix/client/v3/rooms/{self.q(room_id)}/receipt/m.read/{self.q(event_id)}",
                       json_body={})

    async def upload(self, data, content_type, filename):
        resp = await self.req("POST", "/_matrix/media/v3/upload", data=data,
                              params={"filename": filename}, headers={"Content-Type": content_type},
                              timeout=300)
        return resp["content_uri"]

    async def download(self, mxc):
        server, media_id = mxc[len("mxc://"):].split("/", 1)
        try:
            return await self.req("GET", f"/_matrix/client/v1/media/download/{server}/{media_id}",
                                  raw=True, timeout=300)
        except MatrixError:
            return await self.req("GET", f"/_matrix/media/v3/download/{server}/{media_id}",
                                  raw=True, timeout=300)

    async def create_dm(self, user_id):
        resp = await self.req("POST", "/_matrix/client/v3/createRoom", json_body={
            "preset": "trusted_private_chat",
            "is_direct": True,
            "invite": [user_id],
        })
        return resp["room_id"]


# ───────────────────────────── Telegram ─────────────────────────────

class TgError(Exception):
    pass


class Telegram:
    def __init__(self, session):
        self.http = session
        self.base = f"{TG_API_URL}/bot{TG_TOKEN}"

    async def call(self, method, params=None, files=None, timeout=60, retries=6):
        """呼叫 Bot API。被限流會等待；網路斷線或 Telegram 5xx 會重試（間隔 1, 2, 4… 秒）。"""
        delay = 1
        for attempt in range(retries):
            if files:
                form = aiohttp.FormData()
                for k, v in (params or {}).items():
                    if v is not None:
                        form.add_field(k, v if isinstance(v, str) else json.dumps(v))
                for field, (filename, data, ctype) in files.items():
                    form.add_field(field, data, filename=filename, content_type=ctype)
                kwargs = {"data": form}
            else:
                kwargs = {"json": {k: v for k, v in (params or {}).items() if v is not None}}
            try:
                async with self.http.post(f"{self.base}/{method}", timeout=aiohttp.ClientTimeout(total=timeout),
                                          **kwargs) as r:
                    status = r.status
                    body = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                error = f"{type(e).__name__}: {e}"
            else:
                if body.get("ok"):
                    return body["result"]
                retry_after = (body.get("parameters") or {}).get("retry_after")
                if retry_after:
                    await asyncio.sleep(retry_after + 1)
                    continue
                if status < 500:
                    raise TgError(body.get("description", str(body)))
                error = f"HTTP {status}"
            if attempt + 1 < retries:
                log.warning("Telegram %s 失敗（%s），%d 秒後重試", method, error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        raise TgError(f"{method} 重試 {retries} 次仍失敗：{error}")

    async def download(self, file_id):
        info = await self.call("getFile", {"file_id": file_id})
        url = f"{TG_API_URL}/file/bot{TG_TOKEN}/{info['file_path']}"
        async with self.http.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
            r.raise_for_status()
            return await r.read()


# ───────────────────────────── 轉發邏輯 ─────────────────────────────

class Relay:
    def __init__(self, session):
        self.flush_lock = asyncio.Lock()
        self.replaying = False      # 正在補送靜音時段累積的訊息
        self.store = Store(DB_PATH)
        self.mx = Matrix(session, self.store)
        self.tg = Telegram(session)
        self.names = {}       # (room_id, mxid) -> 顯示名稱
        self.last_event = {}  # room_id -> 最後收到的 event_id（回覆時送已讀）
        self.secret_prompt = set()  # bot 正在等你輸入密碼/cookie 的聊天室
        self.topic_locks = {}       # room_id -> asyncio.Lock，避免同一個聊天室重複建主題
        self.self_ghosts = set()    # 代表「你自己」的 bridge 帳號（你在手機 App 發的訊息會用它送進來）
        self.self_checked = 0.0
        self.locked = self.store.get("locked") == "1"  # 群組裡有外人：暫停轉發
        self.requests = {}          # room_id -> 是不是訊息請求（bridge 放在 m.bridge 狀態裡）

    # ---------- 啟動檢查 ----------

    async def check_telegram(self):
        """檢查 Telegram 群組設定；有問題就寫 log 並在群組裡提醒（不會停止運作）。"""
        problems = []
        if TG_OWNER_ID == ANONYMOUS_ADMIN_ID:
            problems.append(f".env 的 TELEGRAM_OWNER_ID={ANONYMOUS_ADMIN_ID}：{ANONYMOUS_HINT}"
                            "（把 TELEGRAM_OWNER_ID 清空、重啟 relay，bot 會回覆你真正的 ID）")
        try:
            me = await self.tg.call("getMe")
            chat = await self.tg.call("getChat", {"chat_id": TG_CHAT_ID})
            if not chat.get("is_forum"):
                problems.append("群組沒有開啟「主題 (Topics)」")
            member = await self.tg.call("getChatMember", {"chat_id": TG_CHAT_ID, "user_id": me["id"]})
            if member.get("status") != "administrator":
                problems.append("bot 不是群組管理員")
            else:
                if not member.get("can_manage_topics"):
                    problems.append("bot 沒有「管理主題」權限")
                if not member.get("can_delete_messages"):
                    problems.append("bot 沒有「刪除訊息」權限（無法自動刪掉你輸入的密碼）")
        except TgError as e:
            log.error("讀不到 Telegram 群組 %s：%s（TELEGRAM_CHAT_ID 對嗎？bot 在群組裡嗎？）", TG_CHAT_ID, e)
            return
        if problems:
            text = "⚠️ 轉發設定有問題：\n" + "\n".join(f"• {p}" for p in problems)
            log.error(text)
            try:
                await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID, "text": text})
            except TgError as e:
                log.error("無法在群組發訊息：%s", e)

    # ---------- 群組成員防護：有外人就暫停轉發 ----------

    LOCK_TEXT = "🔒 群組裡有你和 bot 以外的成員，轉發和指令都暫停中。把多的人移出群組後會自動恢復。"

    async def check_members(self, joined=None):
        """群組成員超過 GROUP_MAX_MEMBERS 就暫停轉發（新訊息先存著），人數恢復後自動補送。"""
        try:
            count = await self.tg.call("getChatMemberCount", {"chat_id": TG_CHAT_ID})
        except Exception as e:  # noqa: BLE001
            log.warning("查詢群組人數失敗，維持目前狀態：%s", e)
            return
        if count > GROUP_MAX_MEMBERS and not self.locked:
            self.locked = True
            self.store.set("locked", "1")
            log.warning("群組有 %s 位成員（上限 %s），暫停轉發", count, GROUP_MAX_MEMBERS)
            who = f"\n新加入：{'、'.join(joined)}" if joined else ""
            await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID, "text":
                f"🔒 群組現在有 {count} 位成員，應該只有你和 bot（{GROUP_MAX_MEMBERS} 位）。{who}\n"
                "為了不讓別人看到你的聊天，轉發已暫停：新訊息先存在你的電腦上，不會送進群組，指令也暫停。\n"
                f"把多的人移出群組後，約 {MEMBER_CHECK_SECONDS} 秒內自動恢復並補送。\n"
                "（已經在群組裡的舊訊息擋不住，必要時請自己刪除）"})
        elif count <= GROUP_MAX_MEMBERS and self.locked:
            self.locked = False
            self.store.set("locked", "")
            log.info("群組人數恢復為 %s，恢復轉發", count)
            await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID, "disable_notification": True,
                                               "text": "🔓 群組只剩你和 bot，已恢復轉發。"})
            if self.store.deferred_count():
                # 管理主題（登入過程）馬上補送；聊天訊息照通知時段規則
                await self.flush_deferred(bot_only=self.holding())

    async def member_guard_loop(self):
        while True:
            await asyncio.sleep(MEMBER_CHECK_SECONDS)
            try:
                await self.check_members()
            except Exception:  # noqa: BLE001
                log.exception("檢查群組成員失敗")

    # ---------- 聊天室 / 主題 ----------

    async def room_info(self, room_id):
        """取得（必要時建立）聊天室資料。回傳 None 代表忽略這個聊天室。"""
        r = self.store.room(room_id)
        if r:
            return None if r["ignored"] else r
        r = {"room_id": room_id, "topic_id": None, "platform": None, "title": "", "is_bot": 0, "ignored": 0}
        state = await self.mx.full_state(room_id)
        create = next((e for e in state if e["type"] == "m.room.create"), {})
        members = {e["state_key"]: e["content"] for e in state if e["type"] == "m.room.member"
                   and e["content"].get("membership") in ("join", "invite") and e["state_key"] != MY_MXID}
        for m in members:
            plat, _ = platform_of(m)
            if plat:
                r["platform"] = plat
                break
        if create.get("content", {}).get("type") == "m.space":
            r["ignored"] = 1
        elif create.get("sender") == MY_MXID:
            # 自己建的聊天室只有跟 bridge bot 的管理私訊；bridge 的聊天室一定是 bridge bot 建的。
            # （不能用「成員只有 bot」判斷：bridge 會先邀請你、之後才把聯絡人加進來）
            r["is_bot"] = 1
            r["title"] = bot_title(r["platform"])
        else:
            name = next((e["content"].get("name") for e in state if e["type"] == "m.room.name"), None)
            if not name:
                await self.refresh_self_ghosts()
                ghosts = [c.get("displayname") or m for m, c in members.items()
                          if not platform_of(m)[1] and m not in self.self_ghosts]
                name = ", ".join(ghosts[:3])
            r["title"] = self.topic_title(r["platform"], name) if name else ""
            r["approved"] = 0 if r["platform"] in APPROVE_PLATFORMS else 1
        self.store.save_room(r)
        return None if r["ignored"] else r

    @staticmethod
    def topic_title(platform, name):
        icon = PLATFORM_ICON.get(platform, "⚪")
        return f"{icon} {name}"[:128]

    async def ensure_topic(self, r, recreate=False):
        # 同一個聊天室同時只能有一個地方在建主題；建主題要等 Telegram 回應，
        # 這段時間內另一則訊息（例如 bridge bot 的招呼）也可能要建，不鎖住就會重複建兩個。
        lock = self.topic_locks.setdefault(r["room_id"], asyncio.Lock())
        async with lock:
            saved = self.store.room(r["room_id"])
            if saved and saved["topic_id"] and (not recreate or saved["topic_id"] != r["topic_id"]):
                r["topic_id"] = saved["topic_id"]  # 已經有了（或別人剛重建好）
                return r["topic_id"]
            if r["topic_id"] and not recreate:
                return r["topic_id"]
            res = await self.tg.call("createForumTopic", {"chat_id": TG_CHAT_ID, "name": r["title"] or "⚪ (未命名)"})
            r["topic_id"] = res["message_thread_id"]
            self.store.save_room(r)
            log.info("建立主題 %s -> %s", r["title"], r["room_id"])
        if r["is_bot"]:
            await self.tg.call("sendMessage", {
                "chat_id": TG_CHAT_ID, "message_thread_id": r["topic_id"],
                "text": "這是 bridge 的管理聊天室。\n輸入 help 看指令、login 開始登入、logout 登出。"
                        + ("\n（Discord：login 會給你 QR code，用手機 Discord 掃描）\n\n"
                           "只會轉發你挑的頻道：\n/servers 列出伺服器，按「➕」加入\n"
                           "/chats 伺服器名 列出那個伺服器的頻道，按「➕」開始轉發\n"
                           "/added 列出已在轉發的頻道" if r["platform"] == "discord" else
                           "\n\n只會轉發你加入的聊天：\n/chats 列出還沒加入的聊天（可加關鍵字，例如 /chats 媽）\n"
                           "/added 列出已加入的聊天" if r["platform"] in APPROVE_PLATFORMS else ""),
            })
        return r["topic_id"]

    async def tg_send(self, r, method, params, files=None):
        """送到聊天室對應的主題；主題被刪掉的話自動重建。通知時段以外靜音送達。"""
        if self.replaying or ("disable_notification" not in params and self.quiet()):
            params = {**params, "disable_notification": True}
        for attempt in range(2):
            topic = await self.ensure_topic(r, recreate=attempt > 0)
            try:
                return await self.tg.call(method, {"chat_id": TG_CHAT_ID, "message_thread_id": topic, **params},
                                          files=files, timeout=300 if files else 60)
            except TgError as e:
                err = str(e).lower()
                if attempt == 0 and ("thread not found" in err or "topic_deleted" in err):
                    log.warning("主題不存在，重新建立：%s", e)
                    continue
                raise

    # ---------- 白名單：只轉發你在管理主題加入的聊天 ----------

    async def hold(self, r, ev):
        """還沒加入白名單的聊天：不轉發、不打擾，只默默保留最近幾則，加入時再補送。"""
        self.store.add_pending(r["room_id"], ev)

    async def is_message_request(self, room_id):
        """bridge 標記的訊息請求（m.bridge 狀態的 channel.com.beeper.message_request）。查不到就當作不是，免得訊息卡住。"""
        if room_id not in self.requests:
            try:
                state = await self.mx.full_state(room_id)
            except MatrixError as e:
                log.warning("讀不到 %s 是不是訊息請求，先照常轉發：%s", room_id, e)
                return False
            self.requests[room_id] = any(
                ((e.get("content") or {}).get("channel") or {}).get("com.beeper.message_request")
                for e in state if e.get("type") in BRIDGE_INFO_TYPES)
        return self.requests[room_id]

    async def on_bridge_info(self, room_id, content):
        """bridge 更新聊天室資訊：訊息請求在 App 裡被接受時，補送先前保留的訊息。"""
        is_request = bool((content.get("channel") or {}).get("com.beeper.message_request"))
        self.requests[room_id] = is_request
        if is_request:
            return
        r = self.store.room(room_id)
        if not r or r["ignored"] or r["is_bot"] or not r.get("approved", 1) or r["platform"] not in HOLD_REQUEST_PLATFORMS:
            return
        held = self.store.pop_pending(room_id)
        if held:
            log.info("訊息請求已接受，補送 %d 則：%s", len(held), r["title"])
        for ev in held:
            await self.on_matrix_event(room_id, ev)

    async def approve(self, r):
        r["approved"] = 1
        self.store.save_room(r)
        for ev in self.store.pop_pending(r["room_id"]):  # 補送加入前收到的訊息
            await self.on_matrix_event(r["room_id"], ev)

    async def platform_rooms(self, platform):
        """這個平台所有的聊天（bridge 建的聊天室都會邀請你，relay 都有加入）。"""
        joined = (await self.mx.req("GET", "/_matrix/client/v3/joined_rooms")).get("joined_rooms", [])
        rooms = []
        for room_id in joined:
            try:
                r = await self.room_info(room_id)
            except MatrixError:
                continue
            if r and not r["is_bot"] and r["platform"] == platform and r["title"]:
                rooms.append(r)
        return sorted(rooms, key=lambda x: x["title"])

    async def list_chats(self, bot_room, msg, added):
        """管理主題裡的 /chats [關鍵字]（還沒加入的）和 /added（已加入的）。
        LINE 的 /chats 除了已經有聊天室的群組 / 私訊，也會列出還沒聊過的好友（bridge 只在有新訊息時才建私訊聊天室）。"""
        platform = bot_room["platform"]
        keyword = ((msg.get("text") or "").split(maxsplit=1)[1:] or [""])[0].strip()
        all_rooms = await self.platform_rooms(platform)
        items = [(r["title"], f"{'rm' if added else 'ok'}:{self.store.room_rowid(r['room_id'])}")
                 for r in all_rooms
                 if bool(r["approved"]) == added and (not keyword or keyword.lower() in r["title"].lower())]
        items.sort(key=lambda x: x[0])
        if not added and platform != "discord":  # Discord bridge 沒有好友清單 API
            known = {r["room_id"] for r in all_rooms}
            friends = []
            try:
                if keyword:
                    contacts = (await self.provision(platform, "POST", "/search_users", {"query": keyword})).get("results")
                else:
                    contacts = (await self.provision(platform, "GET", "/contacts", timeout=60)).get("contacts")
            except Exception as e:  # noqa: BLE001
                log.info("查詢 %s 好友清單失敗：%s", platform, e)
                contacts = []
            for c in contacts or []:
                if c.get("dm_room_mxid") in known or not c.get("id") or len(f"dm:{c['id']}".encode()) > 64:
                    continue  # 已經有聊天室的（上面列過或已加入）就不重複
                friends.append((f"👤 {c.get('name') or c['id']}（好友，還沒聊過）", f"dm:{c['id']}"))
            items += sorted(friends, key=lambda x: x[0])  # 已經有聊天室的排前面，好友排後面
        if not items:
            text = "目前沒有已加入的聊天。" if added else "找不到還沒加入的聊天。"
            return await self.reply(msg, text)
        icon = "➖" if added else "➕"
        text = ("已加入的聊天，按一下移除：" if added else "按一下就加入，之後的訊息會轉過來：")
        if len(items) > 50:
            text += f"\n（共 {len(items)} 個，只列前 50 個，可以用 /chats 關鍵字 縮小範圍）"
        buttons = [[{"text": f"{icon} {title}"[:60], "callback_data": data}] for title, data in items[:50]]
        await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID, "message_thread_id": bot_room["topic_id"],
                                           "text": text, "reply_markup": {"inline_keyboard": buttons}})

    async def add_contact(self, platform, remote_id):
        """請 bridge 跟這位好友建立私訊聊天室（已經有就沿用），加入白名單並開好主題。"""
        resp = await self.provision(platform, "POST", f"/create_dm/{urllib.parse.quote(remote_id, safe='')}")
        room_id = resp.get("dm_room_mxid")
        if not room_id:
            raise RuntimeError("bridge 沒有回傳聊天室")
        for _ in range(10):  # bridge 建好聊天室後會邀請你，邀請可能晚一點才到
            try:
                await self.mx.join(room_id)
                break
            except MatrixError:
                await asyncio.sleep(1)
        r = await self.room_info(room_id)
        if not r["title"]:
            r["title"] = self.topic_title(platform, resp.get("name") or remote_id)
        await self.approve(r)
        await self.ensure_topic(r)
        return r

    async def on_callback(self, cq):
        """處理「➕ 加入 / ➖ 移除」按鈕。"""
        msg = cq.get("message") or {}
        ok = cq.get("from", {}).get("id") == TG_OWNER_ID and msg.get("chat", {}).get("id") == TG_CHAT_ID
        if self.locked:
            return await self.tg.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": self.LOCK_TEXT})
        action, _, rowid = (cq.get("data") or "").partition(":")
        if action in ("gb", "gu"):
            return await self.on_server_callback(cq, msg, ok, action, rowid)
        r = self.store.room_by_rowid(int(rowid)) if ok and rowid.isdigit() else None
        answer = None
        if ok and action == "dm":
            bot_room = self.store.room_by_topic(msg.get("message_thread_id"))
            platform = bot_room["platform"] if bot_room and bot_room["is_bot"] else (APPROVE_PLATFORMS or ["line"])[0]
            await self.tg.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "建立中…"})
            try:
                r = await self.add_contact(platform, rowid)
                text = f"✅ 已加入 {r['title']}，之後的訊息會轉過來，也可以直接在它的主題裡傳訊息"
            except Exception as e:  # noqa: BLE001
                log.exception("加入好友失敗")
                text = f"⚠️ 加入失敗：{e}"
            await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID,
                                               "message_thread_id": msg.get("message_thread_id"), "text": text})
            return
        if r and action == "ok":
            await self.approve(r)
            answer = f"已加入 {r['title']}"
        elif r and action == "rm":
            r["approved"] = 0
            self.store.save_room(r)
            answer = f"已移除 {r['title']}，之後不會再轉過來"
        await self.tg.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": answer})
        if answer:
            await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID,
                                               "message_thread_id": msg.get("message_thread_id"),
                                               "text": ("✅ " if action == "ok" else "➖ ") + answer})

    # ---------- Discord：挑伺服器（/servers），再用 /chats 挑頻道 ----------

    async def list_servers(self, bot_room, msg):
        try:
            guilds = (await self.discord_api("GET", "guilds")).get("guilds") or []
        except Exception as e:  # noqa: BLE001
            return await self.reply(msg, f"⚠️ 查不到伺服器清單（{e}）。先輸入 login 登入 Discord")
        if not guilds:
            return await self.reply(msg, "找不到任何伺服器。剛登入的話等一下再試")
        guilds.sort(key=lambda g: (g.get("bridging_mode") == "nothing", g.get("name") or ""))
        buttons = [[{"text": (f"➕ {g.get('name')}" if g.get("bridging_mode") == "nothing"
                              else f"➖ {g.get('name')}（已加入）")[:60],
                     "callback_data": f"{'gb' if g.get('bridging_mode') == 'nothing' else 'gu'}:{g['id']}"}]
                   for g in guilds[:50]]
        await self.tg.call("sendMessage", {
            "chat_id": TG_CHAT_ID, "message_thread_id": bot_room["topic_id"], "reply_markup": {"inline_keyboard": buttons},
            "text": "按「➕」加入伺服器，再用 /chats 挑要轉發的頻道（伺服器加入了，頻道沒挑也不會轉發）："})

    async def on_server_callback(self, cq, msg, ok, action, guild_id):
        thread = msg.get("message_thread_id")
        await self.tg.call("answerCallbackQuery", {"callback_query_id": cq["id"],
                                                   "text": "處理中…" if ok else None})
        if not ok or not guild_id.isdigit():
            return
        try:
            guilds = (await self.discord_api("GET", "guilds")).get("guilds") or []
            name = next((g.get("name") for g in guilds if g["id"] == guild_id), guild_id)
            if action == "gb":
                await self.discord_api("POST", f"guilds/{guild_id}", {"auto_create_channels": True})
                text = (f"✅ 已加入伺服器 {name}，頻道正在建立。\n"
                        f"等幾秒後輸入 /chats {name} 挑要轉發的頻道")
            else:
                await self.discord_api("DELETE", f"guilds/{guild_id}")
                text = f"➖ 已移除伺服器 {name}，它的頻道都不會再轉過來"
        except Exception as e:  # noqa: BLE001
            text = f"⚠️ 操作失敗：{e}"
        await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID, "message_thread_id": thread, "text": text})

    async def ensure_bot_rooms(self):
        """每個 bridge bot 都要有一個私訊聊天室（Telegram 的「登入 / 管理」主題）。回傳還沒弄好的平台。"""
        missing = []
        for b in BRIDGES:
            room_id = self.store.get(f"botdm:{b}")
            if not room_id:
                bot = f"@{b}bot:{SERVER_NAME}"
                try:
                    # bridge 第一次啟動時才會註冊 bot 帳號；還沒註冊就先別建聊天室
                    await self.mx.req("GET", f"/_matrix/client/v3/profile/{self.mx.q(bot)}")
                    room_id = await self.mx.create_dm(bot)
                except MatrixError as e:
                    log.info("%s 還沒準備好，稍後重試（%s）", bot, e.status)
                    missing.append(b)
                    continue
                self.store.set(f"botdm:{b}", room_id)
            r = self.store.room(room_id)
            if not r:
                r = {"room_id": room_id, "topic_id": None, "platform": b,
                     "title": bot_title(b), "is_bot": 1, "ignored": 0}
                self.store.save_room(r)
            if not r["topic_id"]:
                try:
                    await self.ensure_topic(r)
                except TgError as e:
                    log.error("建立「%s」主題失敗：%s", r["title"], e)
                    missing.append(b)
        return missing

    async def bot_rooms_loop(self):
        while True:
            try:
                if not await self.ensure_bot_rooms():
                    return
            except Exception:  # noqa: BLE001
                log.exception("建立管理聊天室失敗")
            await asyncio.sleep(15)

    # ---------- Matrix -> Telegram ----------

    async def discord_api(self, method, path, body=None):
        """Discord bridge 的 provisioning API（/_matrix/provision/v1/...）。"""
        with open(DISCORD_SECRET_FILE) as f:
            secret = f.read().strip()
        async with self.mx.http.request(method, f"{BRIDGE_URLS['discord']}/_matrix/provision/v1/{path}",
                                        params={"user_id": MY_MXID}, json=body,
                                        headers={"Authorization": f"Bearer {secret}"},
                                        timeout=aiohttp.ClientTimeout(total=60)) as resp:
            data = await resp.json(content_type=None) if resp.status != 204 else {}
            if resp.status >= 300:
                raise RuntimeError((data or {}).get("error") or f"HTTP {resp.status}")
            return data

    async def provision(self, platform, method, path, body=None, timeout=30):
        """呼叫 bridge 的 provisioning API（用 relay 的 Matrix 登入驗證）。"""
        async with self.mx.http.request(method, f"{BRIDGE_URLS[platform]}/_matrix/provision/v3{path}",
                                        params={"user_id": MY_MXID}, json=body,
                                        headers={"Authorization": f"Bearer {self.mx.token}"},
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300:  # create_dm 新建私訊時回 201
                raise RuntimeError(f"HTTP {resp.status}: {data.get('error') if isinstance(data, dict) else data}")
            return data

    async def whoami(self, platform):
        """bridge 的 provisioning API：目前登入的帳號和連線狀態（用 relay 的 Matrix 登入驗證）。"""
        if platform == "discord":  # 舊版 API，換成跟其他 bridge 一樣的格式
            data = await self.discord_api("GET", "ping")
            d = data.get("Discord") or data.get("discord") or {}  # bridge 回的欄位是大寫 Discord
            if not d.get("logged_in"):
                return {"logins": []}
            return {"logins": [{"id": d.get("id"), "name": f"ID {d.get('id')}", "state": {
                "state_event": "CONNECTED" if d.get("connected") else "TRANSIENT_DISCONNECT"}}]}
        async with self.mx.http.get(f"{BRIDGE_URLS[platform]}/_matrix/provision/v3/whoami",
                                    params={"user_id": MY_MXID},
                                    headers={"Authorization": f"Bearer {self.mx.token}"},
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)

    # ---------- 每日登入檢查 ----------

    async def login_report(self):
        """回傳 (有沒有問題, 報告文字)。"""
        lines, problem = [], False
        for b in BRIDGES:
            label = PLATFORM_LABEL[b]
            try:
                logins = (await self.whoami(b)).get("logins") or []
            except Exception as e:  # noqa: BLE001
                lines.append(f"❌ {label}：bridge 沒有回應（{str(e) or type(e).__name__}），試試 docker compose restart {b}")
                problem = True
                continue
            if not logins:
                lines.append(f"⚠️ {label}：還沒登入 → 到「{bot_title(b)}」輸入 login")
                problem = True
            for login in logins:
                state = login.get("state") or {}
                event = state.get("state_event") or login.get("state_event") or "UNKNOWN"
                who = login.get("name") or state.get("remote_name") or login.get("id")
                if event in ("CONNECTED", "BACKFILLING"):
                    lines.append(f"✅ {label}：正常（{who}）")
                elif event in ("CONNECTING", "TRANSIENT_DISCONNECT"):
                    lines.append(f"🟡 {label}：暫時斷線中（{who}），通常會自動恢復；明天還是這樣再重新登入")
                else:
                    reason = state.get("message") or state.get("error") or event
                    lines.append(f"⚠️ {label}：需要重新登入（{who}：{reason}）→ 到「{bot_title(b)}」輸入 login")
                    problem = True
        return problem, "\n".join(lines)

    # ---------- 今天先靜音（/mute、/unmute）、放假（/off、/on） ----------

    def muted_today(self):
        return self.store.get("mute_date") == datetime.now(TAIWAN).strftime("%Y-%m-%d")

    def holiday_notes(self):
        """你指定的放假日（只留今天以後的）：{ISO 日期: 備註}。"""
        raw = self.store.get("holidays") or ""
        try:
            data = json.loads(raw) if raw.startswith("{") else {d: "" for d in raw.split(",") if d}
        except ValueError:
            data = {}
        today = datetime.now(TAIWAN).strftime("%Y-%m-%d")
        return {d: n for d, n in sorted(data.items()) if d >= today}

    def save_holidays(self, notes):
        self.store.set("holidays", json.dumps(dict(sorted(notes.items())), ensure_ascii=False))

    def holidays(self):
        return list(self.holiday_notes())

    def notify_time(self, dt):
        """這個時間點要不要即時轉發：在通知時段內，而且那天沒放假、沒 /mute。"""
        day = dt.strftime("%Y-%m-%d")
        return day not in self.holidays() and self.store.get("mute_date") != day and notify_now(dt)

    def quiet(self):
        """現在是不是靜音時段：不在通知時段、今天放假，或今天按過 /mute。"""
        return not self.notify_time(datetime.now(TAIWAN))

    def preloading(self):
        """靜音時段快結束了（PRELOAD_MINUTES 內就進入通知時段）：提早補送，但仍是靜音送達。"""
        return (PRELOAD_MINUTES > 0 and self.quiet()
                and self.notify_time(datetime.now(TAIWAN) + timedelta(minutes=PRELOAD_MINUTES)))

    def holding(self):
        """新訊息要不要先存起來：群組有外人，或靜音時段而且還沒到提早補送的時間。"""
        return self.locked or (self.quiet() and not self.preloading())

    async def flush_deferred(self, bot_only=False):
        """把靜音時段累積的訊息照順序補送（每則都靜音），最後發一則摘要。bot_only：只補送管理主題的。"""
        async with self.flush_lock:
            count, rooms = 0, set()
            self.replaying = True
            try:
                while item := self.store.next_deferred(bot_only):
                    seq, room_id, ev = item
                    try:
                        await self.on_matrix_event(room_id, ev, replay=True)
                    except Exception:  # noqa: BLE001
                        log.exception("補送訊息失敗 %s %s", room_id, ev.get("event_id"))
                    self.store.del_deferred(seq)
                    is_edit = ((ev.get("content") or {}).get("m.relates_to") or {}).get("rel_type") == "m.replace"
                    if ev.get("type") in ("m.room.message", "m.sticker") and not is_edit:
                        count += 1
                        rooms.add(room_id)
            finally:
                self.replaying = False
            if count and not bot_only:
                await self.tg.call("sendMessage", {
                    "chat_id": TG_CHAT_ID, "disable_notification": self.quiet(),
                    "text": f"📬 靜音期間有 {count} 則新訊息（{len(rooms)} 個聊天），已補送到各自的主題"
                            + ("（提早載入）" if self.preloading() else "")})
            return count

    async def deferred_loop(self):
        """通知時段一到（或快到了），自動補送。"""
        while True:
            await asyncio.sleep(30)
            try:
                if not self.holding() and self.store.deferred_count():
                    await self.flush_deferred()
            except Exception:  # noqa: BLE001
                log.exception("補送靜音期間的訊息失敗")

    async def handle_mute(self, msg, text):
        """/mute：今天剩下的時間轉發的訊息都靜音（半夜自動恢復）；/unmute：提早恢復。回傳是否處理了。"""
        cmd = text.split()[:1]
        today = datetime.now(TAIWAN).strftime("%Y-%m-%d")
        if cmd == ["/mute"]:
            self.store.set("mute_date", today)
            await self.reply(msg, f"🔕 今天（{today[5:].replace('-', '/')}）接下來的訊息先不送進來，明天到了通知時段再一次補送。\n"
                                  "想看就輸入 /inbox，想恢復就輸入 /unmute")
        elif cmd == ["/unmute"]:
            was = self.muted_today()
            self.store.set("mute_date", "")
            if self.quiet():
                await self.reply(msg, "🔔 已取消今天的靜音" + ("" if was else "（本來就沒有）")
                                      + "，不過現在不在通知時段，訊息還是會先存著。想看就輸入 /inbox")
            else:
                await self.reply(msg, "🔔 已恢復通知" + ("" if was else "（本來就沒有靜音）"))
                await self.flush_deferred()
        elif cmd in (["/off"], ["/on"]):
            await self.handle_holiday(msg, cmd[0], text.split()[1:])
        elif cmd == ["/inbox"]:
            if not self.store.deferred_count():
                await self.reply(msg, "📭 目前沒有累積的訊息")
            else:
                await self.flush_deferred()
        else:
            return False
        return True

    @staticmethod
    def parse_days(args):
        """「10/10」「2026/10/10」「10/10-10/12」「10/10~10/12」開頭的日期，後面的字當備註。
        回傳 (日期清單, 備註)；看不懂日期回傳 (None, "")。"""
        today = datetime.now(TAIWAN).date()

        def one(tok, after=None):
            parts = [int(x) for x in re.split(r"[/.]", tok)]
            if len(parts) == 3:
                return datetime(*parts).date()
            if len(parts) != 2:
                raise ValueError(tok)
            d = datetime(today.year, *parts).date()
            base = after or today
            return d.replace(year=d.year + 1) if d < base else d

        days, i = [], 0
        for i, arg in enumerate(args):
            if not re.match(r"\d", arg):  # 不是數字開頭：從這裡開始是備註
                break
            arg = re.sub(r"(\d{4})-(\d{1,2})-(\d{1,2})", r"\1/\2/\3", arg)  # 2026-10-10 → 2026/10/10
            m = re.fullmatch(r"(.+?)[~～-](.+)", arg)  # 範圍
            try:
                start = one(m.group(1) if m else arg)
                end = one(m.group(2), after=start) if m else start
            except (ValueError, TypeError):
                return None, ""
            if end < start or (end - start).days >= HOLIDAY_MAX_DAYS:
                return None, ""
            days += [start + timedelta(days=j) for j in range((end - start).days + 1)]
        else:
            i = len(args)
        note = " ".join(args[i:])[:50]
        return ([d.strftime("%Y-%m-%d") for d in days] or None), note

    @staticmethod
    def fmt_day(d, short=False):
        """10/10（六）；不是今年的加上年份（short=True 時不加，例如清單裡月份標題已經有年份）。"""
        wd = "一二三四五六日"
        this_year = datetime.now(TAIWAN).strftime("%Y")
        return (f"{d[5:].replace('-', '/') if short or d[:4] == this_year else d.replace('-', '/')}"
                f"（{wd[datetime.strptime(d, '%Y-%m-%d').weekday()]}）")

    @classmethod
    def fmt_days(cls, days):
        return "、".join(cls.fmt_day(d) for d in days)

    @classmethod
    def fmt_holiday_list(cls, notes):
        """依月份分組；同一個月裡連續、備註一樣的日子合成一段。"""
        this_year = datetime.now(TAIWAN).strftime("%Y")
        lines, month, run = [], None, []

        def flush():
            if run:
                span = cls.fmt_day(run[0], True) + (f"～{cls.fmt_day(run[-1], True)}" if len(run) > 1 else "")
                note = notes[run[0]]
                lines.append(f"  {span}" + (f"  {note}" if note else ""))

        for d in sorted(notes):
            y, m = d[:4], int(d[5:7])
            if (y, m) != month:
                flush()
                run = []
                month = (y, m)
                lines.append(f"\n📅 {m} 月" if y == this_year else f"\n📅 {y} 年 {m} 月")
            prev = run[-1] if run else None
            if prev and (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(prev, "%Y-%m-%d")).days == 1 \
                    and notes[prev] == notes[d]:
                run.append(d)
            else:
                flush()
                run = [d]
        flush()
        return "\n".join(lines).strip("\n")

    async def handle_holiday(self, msg, cmd, args):
        """/off 日期 [備註]：那天整天放假（訊息先存著，下一個通知時段補送）；/on 日期：取消；/off 不帶日期：列出。"""
        notes = self.holiday_notes()
        if not args:
            if cmd == "/on":
                return await self.reply(msg, "要取消哪天？例如 /on 10/10。目前的假日：輸入 /off 查看")
            text = (f"🏖 已設定的假日：\n{self.fmt_holiday_list(notes)}" if notes else "目前沒有設定假日")
            return await self.reply(msg, text + "\n\n加假日：/off 10/10 國慶日、/off 10/9-10/12 出國，取消：/on 10/10")
        days, note = self.parse_days(args)
        if not days:
            return await self.reply(msg, f"看不懂日期。範例：{cmd} 10/10、{cmd} 10/10-10/12 出國、{cmd} 2027/1/2、"
                                         f"{cmd} 2026/12/30-2027/1/2"
                                         f"（一次最多 {HOLIDAY_MAX_DAYS} 天，日期後面可以寫備註）")
        today = datetime.now(TAIWAN).strftime("%Y-%m-%d")
        if cmd == "/off":
            past = [d for d in days if d < today]
            if len(past) == len(days):
                return await self.reply(msg, f"{self.fmt_days(past)} 已經過了，沒有設定")
            new = [d for d in days if d >= today]
            for d in new:
                notes[d] = note or notes.get(d, "")  # 沒寫備註就保留原本的
            self.save_holidays(notes)
            text = (f"🏖 放假：\n{self.fmt_holiday_list({d: notes[d] for d in new})}\n"
                    "這幾天的訊息先存著，下一個通知時段補送。")
            if past:
                text += f"\n（{self.fmt_days(past)} 已經過了，略過）"
        else:
            removed = [d for d in days if d in notes]
            for d in removed:
                notes.pop(d)
            self.save_holidays(notes)
            text = (f"📅 已取消放假：{self.fmt_days(removed)}" if removed else "這些日子本來就沒有設定放假")
        await self.reply(msg, text)
        if not self.holding() and self.store.deferred_count():
            await self.flush_deferred()  # 例如取消今天的假，而且現在在通知時段內

    async def send_login_report(self, title):
        if self.locked:
            return await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID, "text": f"{title}\n{self.LOCK_TEXT}"})
        problem, text = await self.login_report()
        today_note = self.holiday_notes().get(datetime.now(TAIWAN).strftime("%Y-%m-%d"))
        if today_note is not None:
            text += f"\n🏖 今天放假{'：' + today_note if today_note else ''}（/on 今天日期 取消）"
        if self.muted_today():
            text += "\n🔕 今天已靜音（/unmute 恢復）"
        if n := self.store.deferred_count():
            text += f"\n📬 有 {n} 則訊息等著補送（/inbox 馬上送出）"
        await self.tg.call("sendMessage", {"chat_id": TG_CHAT_ID, "text": f"{title}\n{text}",
                                           "disable_notification": not problem})

    async def login_check_loop(self):
        if not LOGIN_CHECK_TIME:
            return
        try:
            hour, minute = (int(x) for x in LOGIN_CHECK_TIME.split(":"))
        except ValueError:
            log.error("LOGIN_CHECK_TIME 格式要像 09:00，目前是 %r，不做每日檢查", LOGIN_CHECK_TIME)
            return
        # 每 20 秒看一次時鐘，不一次睡到隔天：Docker 虛擬機的計時器可能跑快或跑慢，睡一整天會提早醒來
        # （曾在 08:58 提早送一次、09:00 又送一次）。每天只送一次，送過的日期記在資料庫，重啟也不會重送；
        # relay 在檢查時間沒在跑的話，一小時內啟動會補送，超過就等明天。
        while True:
            await asyncio.sleep(20)
            now = datetime.now(TAIWAN)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            today = now.strftime("%Y-%m-%d")
            if not (target <= now < target + timedelta(hours=1)) or self.store.get("login_check_date") == today:
                continue
            try:
                await self.send_login_report(f"🔍 每日登入檢查（{LOGIN_CHECK_TIME}）")
            except Exception:  # noqa: BLE001
                log.exception("每日登入檢查失敗，稍後重試")
                continue
            self.store.set("login_check_date", today)

    async def refresh_self_ghosts(self, force=False):
        """問各 bridge 目前登入的是哪些帳號，換算成代表你自己的 Matrix 帳號。最多 30 秒查一次。"""
        if not force and time.time() - self.self_checked < 30:
            return
        self.self_checked = time.time()
        found = set()
        for b in BRIDGES:
            try:
                data = await self.whoami(b)
                for login in data.get("logins") or []:
                    found.add(ghost_mxid(b, login["id"]))
            except Exception as e:  # noqa: BLE001
                log.debug("查詢 %s 登入帳號失敗：%s", b, e)
        if found != self.self_ghosts:
            log.info("你自己的帳號：%s", ", ".join(sorted(found)) or "（尚未登入）")
        self.self_ghosts = found

    async def display_name(self, room_id, mxid):
        key = (room_id, mxid)
        if key not in self.names:
            member = await self.mx.state(room_id, "m.room.member", mxid) or {}
            self.names[key] = member.get("displayname") or mxid
        return self.names[key]

    async def on_matrix_event(self, room_id, ev, replay=False):
        etype = ev.get("type")
        sender = ev.get("sender", "")
        content = ev.get("content", {})
        if etype == "m.room.member" and ev.get("state_key"):
            self.names.pop((room_id, ev["state_key"]), None)
            return
        if etype in BRIDGE_INFO_TYPES and "state_key" in ev:
            await self.on_bridge_info(room_id, content)
            return
        if sender == MY_MXID or etype not in ("m.room.message", "m.sticker", "m.reaction",
                                               "m.room.encrypted", "m.room.name"):
            return
        r = await self.room_info(room_id)
        if not r:
            return
        if etype == "m.room.name":
            if not r["is_bot"] and content.get("name"):
                r["title"] = self.topic_title(r["platform"], content["name"])
                self.store.save_room(r)
                if r["topic_id"]:
                    try:
                        await self.tg.call("editForumTopic", {"chat_id": TG_CHAT_ID,
                                                              "message_thread_id": r["topic_id"],
                                                              "name": r["title"]})
                    except TgError as e:
                        log.warning("改主題名稱失敗：%s", e)
            return

        # 靜音時段（通知時段以外 / /mute）先不送，免得跳通知；時間到再照順序補送。
        # 佇列裡還有訊息時，新訊息也要排在後面，順序才不會亂。登入 / 管理主題不受影響。
        if not r.get("approved", 1):  # 白名單外的聊天：不管是不是靜音時段，都只默默保留
            await self.hold(r, ev)
            return
        if not r["is_bot"] and r["platform"] in HOLD_REQUEST_PLATFORMS and await self.is_message_request(room_id):
            await self.hold(r, ev)  # 訊息請求（IG：沒追蹤的人）：在 App 裡接受之前都默默保留
            return
        # 群組有外人時，連管理主題（登入 QR code、帳號資訊）也先存著
        if not replay and (not r["is_bot"] or self.locked) and (self.holding() or self.store.deferred_count()):
            self.store.add_deferred(room_id, ev)
            if not self.holding():
                asyncio.create_task(self.flush_deferred())
            return

        is_self = False
        if not r["is_bot"] and not platform_of(sender)[1]:
            if sender not in self.self_ghosts:
                await self.refresh_self_ghosts()
            is_self = sender in self.self_ghosts
        if not is_self:
            self.last_event[room_id] = ev["event_id"]
        name = "我（從手機）" if is_self else await self.display_name(room_id, sender)
        if r["is_bot"]:
            body = content.get("body", "")
            if PROMPT_RE.search(body):
                if SECRET_RE.search(body):
                    self.secret_prompt.add(room_id)
                else:
                    self.secret_prompt.discard(room_id)
            elif LOGIN_END_RE.search(body):
                self.secret_prompt.discard(room_id)
        elif not r["title"] and not is_self:
            r["title"] = self.topic_title(r["platform"], name)
            self.store.save_room(r)
        # 私聊時主題名稱就是對方名字，不用每則訊息都重複；群組才標示發言者
        show_name = not r["is_bot"] and self.topic_title(r["platform"], name) != r["title"]
        header = f"<b>{esc(name[:64])}</b>\n" if show_name else ""
        relates = content.get("m.relates_to") or {}

        if etype == "m.reaction":
            target = self.store.tg_for_event(relates.get("event_id", ""))
            if target:
                await self.tg_send(r, "sendMessage", {
                    "text": f"{esc(name)} 回應了 {esc(relates.get('key', ''))}",
                    "parse_mode": "HTML", "disable_notification": True,
                    "reply_parameters": {"message_id": target, "allow_sending_without_reply": True},
                })
            return
        if etype == "m.room.encrypted":
            await self.tg_send(r, "sendMessage", {"text": header + "🔒（加密訊息，無法解密）", "parse_mode": "HTML"})
            return

        reply_to = None
        shown_id = ev["event_id"]  # 記錄「送出了什麼文字」用的 ID：編輯要記在原訊息上
        is_reply = bool(relates.get("m.in_reply_to"))
        if relates.get("rel_type") == "m.replace":
            content = content.get("m.new_content", content)
            shown_id = relates.get("event_id", "")
            fp = text_fingerprint(content)
            if fp == self.store.shown_fp(shown_id):
                # 文字跟上次送出的一樣（例如 Discord 只是更新了連結預覽）：不是你或對方真的改了字，不轉
                log.info("略過文字沒變的編輯 %s", ev.get("event_id"))
                return
            reply_to = self.store.tg_for_event(shown_id)
            header += "✏️ 已編輯：\n"
        else:
            fp = text_fingerprint(content, is_reply)
            if is_reply:
                reply_to = self.store.tg_for_event(relates["m.in_reply_to"].get("event_id", ""))
        reply_params = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None

        msgtype = "m.sticker" if etype == "m.sticker" else content.get("msgtype", "m.text")
        body = content.get("body", "")
        if is_reply:  # 只有回覆才可能帶引用的舊訊息；其他以 > 開頭的是內容本身（例如 Discord 的嵌入卡片）
            body = strip_reply_fallback(body)
        sent = []
        if msgtype in ("m.image", "m.video", "m.audio", "m.file", "m.sticker") and content.get("url"):
            sent = await self.forward_media(r, msgtype, content, header, body, reply_params)
        else:
            if msgtype == "m.emote":
                body = f"* {name} {body}"
            elif msgtype == "m.location":
                body = f"📍 {body}\n{content.get('geo_uri', '')}"
            if not body.strip():
                body = "（空白訊息）"
            for i, chunk in enumerate(html_chunks(body, TG_TEXT_LIMIT - tg_len(header))):
                sent.append(await self.tg_send(r, "sendMessage", {
                    "text": (header if i == 0 else "") + chunk, "parse_mode": "HTML",
                    "reply_parameters": reply_params if i == 0 else None,
                    "link_preview_options": {"is_disabled": True},
                }))
        for m in sent:
            self.store.save_msg(m["message_id"], room_id, ev["event_id"])
        if sent:
            self.store.set_shown_fp(shown_id, fp)

    async def forward_media(self, r, msgtype, content, header, body, reply_params):
        info = content.get("info", {})
        filename = content.get("filename") or body or "file"
        caption_text = body if content.get("filename") and body != content.get("filename") else ""
        caption = header + html_fit(caption_text, TG_CAPTION_LIMIT - tg_len(header)) if caption_text else header
        note = None
        if info.get("size", 0) > TG_UPLOAD_LIMIT:
            note = "檔案太大"
        else:
            try:
                data, ctype = await self.mx.download(content["url"])
            except Exception as e:  # noqa: BLE001
                log.warning("下載 %s 失敗：%s", content["url"], e)
                note = "檔案下載失敗"
        if note:
            return [await self.tg_send(r, "sendMessage", {
                "text": caption + f"\n📎 {esc(filename)}（{note}，請到手機上看）", "parse_mode": "HTML",
                "reply_parameters": reply_params})]
        ctype = info.get("mimetype") or ctype
        if msgtype in ("m.image", "m.sticker") and ctype != "image/gif":
            method, field = "sendPhoto", "photo"
        elif msgtype == "m.image":
            method, field = "sendAnimation", "animation"
        elif msgtype == "m.video":
            method, field = "sendVideo", "video"
        elif msgtype == "m.audio" and ("org.matrix.msc3245.voice" in content or "ogg" in ctype):
            method, field = "sendVoice", "voice"
        elif msgtype == "m.audio":
            method, field = "sendAudio", "audio"
        else:
            method, field = "sendDocument", "document"
        params = {"caption": caption or None, "parse_mode": "HTML", "reply_parameters": reply_params}
        try:
            return [await self.tg_send(r, method, params, files={field: (filename, data, ctype)})]
        except TgError as e:
            if method == "sendDocument":
                raise
            log.info("%s 失敗（%s），改用檔案方式傳送", method, e)
            return [await self.tg_send(r, "sendDocument", params, files={"document": (filename, data, ctype)})]

    async def sync_loop(self):
        since = self.store.get("since")
        first = since is None
        while True:
            try:
                params = {"filter": SYNC_FILTER, "timeout": "0" if first else "30000"}
                if since:
                    params["since"] = since
                resp = await self.mx.req("GET", "/_matrix/client/v3/sync", params=params, timeout=90)
            except Exception as e:  # noqa: BLE001
                log.warning("sync 失敗：%s", e)
                if isinstance(e, MatrixError) and e.status == 401:
                    try:
                        await self.mx.login()
                    except Exception as e2:  # noqa: BLE001
                        log.warning("重新登入 Matrix 失敗：%s", e2)
                await asyncio.sleep(5)
                continue
            rooms = resp.get("rooms", {})
            for room_id in rooms.get("invite", {}):
                try:
                    await self.mx.join(room_id)
                    log.info("已加入聊天室 %s", room_id)
                except MatrixError as e:
                    log.warning("加入 %s 失敗：%s", room_id, e)
            if not first:  # 第一次同步只記位置，不轉發舊訊息
                for room_id, room in rooms.get("join", {}).items():
                    for ev in room.get("timeline", {}).get("events", []):
                        try:
                            await self.on_matrix_event(room_id, ev)
                        except Exception:  # noqa: BLE001
                            log.exception("轉發 Matrix 事件失敗 %s %s", room_id, ev.get("event_id"))
            since = resp["next_batch"]
            self.store.set("since", since)
            first = False

    # ---------- Telegram -> Matrix ----------

    async def on_tg_message(self, msg):
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        if TG_CHAT_ID is None or TG_OWNER_ID is None:
            if user_id == ANONYMOUS_ADMIN_ID:
                text = f"⚠️ 你現在是匿名發言，抓不到你的使用者 ID。\n{ANONYMOUS_HINT}"
            else:
                text = (f"TELEGRAM_CHAT_ID={chat_id}\nTELEGRAM_OWNER_ID={user_id}\n\n"
                        "把這兩行填進 .env 後執行 docker compose up -d relay")
            await self.tg.call("sendMessage", {
                "chat_id": chat_id, "message_thread_id": msg.get("message_thread_id"), "text": text})
            return
        if chat_id == TG_CHAT_ID and ("new_chat_members" in msg or "left_chat_member" in msg):
            joined = [m.get("first_name") or m.get("username") or str(m.get("id"))
                      for m in msg.get("new_chat_members", []) if m.get("id") != TG_OWNER_ID]
            await self.check_members(joined or None)
            return
        if chat_id != TG_CHAT_ID or user_id != TG_OWNER_ID:
            return
        if self.locked and (msg.get("text") or "").startswith("/"):
            # 有外人時不回任何查詢結果（聊天清單、假日備註、登入帳號都可能是隱私）；
            # 一般回覆照常送出，對方的回應會先存著
            await self.reply(msg, self.LOCK_TEXT)
            return
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        r = self.store.room_by_topic(thread) if thread else None
        if not r:
            text = msg.get("text") or ""
            if thread is None and await self.handle_mute(msg, text):
                pass
            elif thread is None and text.split()[:1] == ["/status"]:
                await self.send_login_report("🔍 登入狀態")
            elif thread is None and text.startswith("/"):
                await self.tg.call("sendMessage", {
                    "chat_id": chat_id,
                    "text": "每個聊天對象都會是一個主題，直接在主題裡打字就會回覆過去。\n"
                            "登入：到「🤖 … 登入 / 管理」主題輸入 login。\n"
                            "/status：檢查各平台登入狀態"
                            + (f"（每天 {LOGIN_CHECK_TIME} 也會自動檢查）" if LOGIN_CHECK_TIME else "")
                            + "\n/mute：今天接下來的訊息先存著（明天到了通知時段再補送）、/unmute：恢復"
                            + "\n/inbox：馬上送出靜音期間累積的訊息"
                            + "\n/off 10/10 備註（或 /off 10/10-10/12 出國）：那幾天放假，訊息先存著；/on 10/10 取消；/off 列出假日"
                            + (f"\n{'、'.join(PLATFORM_LABEL[p] for p in APPROVE_PLATFORMS)} 只轉發你加入的聊天："
                               "到它的管理主題輸入 /chats 加入、/added 查看或移除。" if APPROVE_PLATFORMS else ""),
                })
            elif thread is not None:
                await self.reply(msg, "⚠️ 這個主題沒有對應的聊天室")
            return

        text = msg.get("text") or ""
        if r["is_bot"] and r["platform"] in APPROVE_PLATFORMS and text.split()[:1] in (["/chats"], ["/added"]):
            await self.list_chats(r, msg, added=text.startswith("/added"))
            return
        if r["is_bot"] and r["platform"] == "discord" and text.split()[:1] == ["/servers"]:
            await self.list_servers(r, msg)
            return
        if r["is_bot"] and await self.handle_mute(msg, text):
            return
        content = await self.tg_to_matrix_content(msg)
        if content is None:
            await self.reply(msg, "⚠️ 不支援這種訊息")
            return
        if r["is_bot"] and content["msgtype"] == "m.text":
            # 管理主題裡打的字一律加上 bridge 指令前綴（!fb / !ig / !line）。
            # 這樣就算 bridge 認定的「管理聊天室」是別的房間（例如重設過 relay），指令和登入資料照樣有效。
            prefix = f"!{r['platform']}"
            if not content["body"].lower().startswith(prefix):
                content["body"] = f"{prefix} {content['body']}"
            cmd = content["body"].split()[1:2]
            if r["platform"] == "discord" and cmd == ["login"]:
                content["body"] = f"{prefix} login-qr"  # Discord bridge 沒有 login，改用掃 QR code 登入
            elif r["platform"] == "discord" and cmd == ["login-token"]:
                self.secret_prompt.add(r["room_id"])  # 訊息裡就是 token，送出後刪掉
        caption = None
        if content.get("filename") and not await self.caption_supported(r["room_id"], content):
            # 這個平台的這種檔案不能附說明文字（LINE 會拒絕、IG 會直接丟掉）：拆成檔案、文字兩則
            caption = content["body"]
            content["body"] = content.pop("filename")
        reply = msg.get("reply_to_message")
        if reply and reply.get("message_id") != thread:
            _, target = self.store.event_for_tg(reply["message_id"])
            if target:
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": target}}
        try:
            event_id = await self.mx.send(r["room_id"], content)
        except MatrixError as e:
            await self.reply(msg, f"⚠️ 傳送失敗：{e}")
            return
        self.store.save_msg(msg["message_id"], r["room_id"], event_id)
        # 記住送出的文字：平台之後補上連結預覽時，bridge 會送一個文字沒變的「編輯」回來，比對後就不轉
        self.store.set_shown_fp(event_id, text_fingerprint(content))
        if caption:
            caption_content = {"msgtype": "m.text", "body": caption}
            try:
                caption_id = await self.mx.send(r["room_id"], caption_content)
                self.store.set_shown_fp(caption_id, text_fingerprint(caption_content))
            except MatrixError as e:
                await self.reply(msg, f"⚠️ 檔案送出了，但說明文字傳送失敗：{e}")
        if r["room_id"] in self.secret_prompt:
            self.secret_prompt.discard(r["room_id"])
            try:
                await self.tg.call("deleteMessage", {"chat_id": TG_CHAT_ID, "message_id": msg["message_id"]})
                await self.tg_send(r, "sendMessage", {"text": "🔒 已送出，並從 Telegram 刪除剛剛那則含密碼 / cookie 的訊息"})
            except TgError as e:
                await self.reply(msg, f"⚠️ 無法自動刪除這則訊息，請手動刪除：{e}")
        last = self.last_event.get(r["room_id"])
        if last:
            try:
                await self.mx.receipt(r["room_id"], last)
            except MatrixError as e:
                log.debug("送已讀失敗：%s", e)

    async def caption_supported(self, room_id, content):
        """bridge 有沒有說這種檔案可以附說明文字（bridge 放在聊天室裡的 com.beeper.room_features）。
        caption：2 完整支援、1 部分支援、0 不支援、-1 會被丟掉（IG）、-2 會被拒絕（LINE）。
        沒有這項資訊的 bridge（例如 Discord）照舊一起送。"""
        try:
            state = await self.mx.full_state(room_id)
        except MatrixError as e:
            log.debug("讀不到聊天室功能，說明文字照舊一起送：%s", e)
            return True
        features = next((e.get("content") for e in state if e.get("type") == "com.beeper.room_features"), None)
        if not features:
            return True
        cap_type = content.get("msgtype")
        if cap_type == "m.audio" and "org.matrix.msc3245.voice" in content:
            cap_type = "org.matrix.msc3245.voice"
        return ((features.get("file") or {}).get(cap_type) or {}).get("caption", 0) >= 1

    async def tg_to_matrix_content(self, msg):
        text = msg.get("text") or msg.get("caption")
        sticker = msg.get("sticker")
        if sticker and (sticker.get("is_animated") or sticker.get("is_video")):
            # 動態貼圖（.tgs / .webm）FB、IG、LINE 都顯示不了，改送貼圖代表的 emoji
            return {"msgtype": "m.text", "body": sticker.get("emoji") or "[貼圖]"}
        media = None
        if msg.get("photo"):
            media = ("m.image", msg["photo"][-1], "photo.jpg", "image/jpeg")
        else:
            for key, msgtype, default_name, default_type in (
                    ("animation", "m.video", "animation.mp4", "video/mp4"),
                    ("video", "m.video", "video.mp4", "video/mp4"),
                    ("video_note", "m.video", "video.mp4", "video/mp4"),
                    ("voice", "m.audio", "voice.ogg", "audio/ogg"),
                    ("audio", "m.audio", "audio.mp3", "audio/mpeg"),
                    ("sticker", "m.image", "sticker.webp", "image/webp"),
                    ("document", "m.file", "file", "application/octet-stream")):
                if msg.get(key):
                    obj = msg[key]
                    name = obj.get("file_name") or default_name
                    ctype = obj.get("mime_type") or mimetypes.guess_type(name)[0] or default_type
                    if key == "document" and ctype.startswith("image/"):
                        msgtype = "m.image"
                    media = (msgtype, obj, name, ctype)
                    break
        if media is None:
            return {"msgtype": "m.text", "body": text} if text else None

        msgtype, obj, filename, ctype = media
        data = await self.tg.download(obj["file_id"])  # Bot API 下載上限 20MB
        url = await self.mx.upload(data, ctype, filename)
        info = {"mimetype": ctype, "size": len(data)}
        for k in ("width", "height"):
            if obj.get(k):
                info[k] = obj[k]
        if obj.get("duration"):
            info["duration"] = obj["duration"] * 1000
        content = {"msgtype": msgtype, "body": filename, "url": url, "info": info}
        if msg.get("voice"):
            content["org.matrix.msc3245.voice"] = {}
        if text:
            content["body"] = text
            content["filename"] = filename
        return content

    async def reply(self, msg, text):
        await self.tg.call("sendMessage", {
            "chat_id": msg["chat"]["id"], "message_thread_id": msg.get("message_thread_id"), "text": text,
            "reply_parameters": {"message_id": msg["message_id"], "allow_sending_without_reply": True},
        })

    async def tg_loop(self):
        offset = None
        while True:
            try:
                updates = await self.tg.call("getUpdates", {
                    "offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]}, timeout=70, retries=1)
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram getUpdates 失敗：%s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "callback_query" in u and TG_CHAT_ID is not None:
                    try:
                        await self.on_callback(u["callback_query"])
                    except Exception:  # noqa: BLE001
                        log.exception("處理按鈕失敗")
                if "message" in u:
                    try:
                        await self.on_tg_message(u["message"])
                    except Exception as e:  # noqa: BLE001
                        log.exception("處理 Telegram 訊息失敗")
                        try:
                            await self.reply(u["message"], f"⚠️ 轉發失敗：{e}")
                        except Exception:  # noqa: BLE001
                            pass

    async def run(self):
        while True:
            try:
                await self.mx.login()
                break
            except Exception as e:  # noqa: BLE001
                log.warning("登入 Matrix 失敗，稍後重試：%s", e)
                await asyncio.sleep(5)
        log.info("已登入 Matrix：%s（平台：%s）", MY_MXID, ", ".join(BRIDGES))
        if TG_CHAT_ID is None or TG_OWNER_ID is None:
            log.warning("尚未設定 TELEGRAM_CHAT_ID / TELEGRAM_OWNER_ID：在群組講一句話，bot 會回覆 ID")
            await self.tg_loop()
            return
        await self.check_telegram()
        await self.check_members()
        await asyncio.gather(self.sync_loop(), self.tg_loop(), self.bot_rooms_loop(), self.login_check_loop(),
                             self.deferred_loop(), self.member_guard_loop())


async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async with aiohttp.ClientSession() as session:
        await Relay(session).run()


if __name__ == "__main__":
    asyncio.run(main())
