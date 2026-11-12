#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
টেলিগ্রাম ফোর্সড-জয়েন গেট বট
----------------------------
কাজ: ব্যবহারকারী আপনার চ্যানেলে জয়েন না করলে ফাইল/লিংক পাবে না।
ফাইল বটে পাঠালে সেটা সেভ হয়ে যায়, আর প্রতিটার জন্য একটা গেটেড লিংক তৈরি হয়।

প্রয়োজনীয় env:
  BOT_TOKEN   = BotFather থেকে নেওয়া টোকেন
  ADMIN_IDS   = কমা দিয়ে আপনার টেলিগ্রাম আইডি (যেমন 8012564523)
  CHANNEL     = (অপশনাল) @channelusername বা -100xxxxxxxxxx
  CHANNEL_LINK= (অপশনাল) জয়েন বাটনের লিংক
  STORAGE     = (অপশনাল) যে চ্যানেলে ফাইল জমা থাকবে
"""

import os
import re
import io
import time
import random
import sqlite3
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gate")

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
ADMIN_IDS = {int(x) for x in re.findall(r"\d{5,}", os.environ.get("ADMIN_IDS", ""))}
DB_PATH = os.environ.get("DB_PATH", "data/gate.db")
API = "https://api.telegram.org/bot%s/" % BOT_TOKEN
SESSION = requests.Session()

RATE_LIMIT = 12          # প্রতি মিনিটে একজন ব্যবহারকারীর সর্বোচ্চ অনুরোধ
RATE_WINDOW = 60


# ------------------------------------------------------------------ টেলিগ্রাম API
class Resp:
    def __init__(self, ok, result=None, description=None, code=None):
        self.ok, self.result, self.description, self.code = ok, result, description, code


def api(method, files=None, **params):
    for attempt in range(3):
        try:
            r = SESSION.post(API + method, json=params, files=files, timeout=70)
            data = r.json()
            if data.get("ok"):
                return Resp(True, data.get("result"))
            desc = data.get("description", "")
            code = data.get("error_code")
            if code == 429:
                wait = int(data.get("parameters", {}).get("retry_after", 3))
                time.sleep(wait)
                continue
            if code and code >= 500:
                time.sleep(2 + attempt * 2)
                continue
            log.warning("API %s failed: %s", method, desc)
            return Resp(False, None, desc, code)
        except Exception as e:  # network / timeout
            log.warning("API %s error: %s", method, e)
            time.sleep(2 + attempt * 2)
    return Resp(False, None, "network", None)


# ------------------------------------------------------------------ ডেটাবেস
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS files(
                slug TEXT PRIMARY KEY, title TEXT, kind TEXT, file_id TEXT,
                chat_id TEXT, msg_id INTEGER, caption TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                joined_at INTEGER, last_seen INTEGER, downloads INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER,
                event TEXT, detail TEXT);
            """
        )


def get_setting(k, default=""):
    with db() as c:
        row = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default


def set_setting(k, v):
    with db() as c:
        c.execute(
            "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )


def log_event(uid, event, detail=""):
    with db() as c:
        c.execute(
            "INSERT INTO events(ts,user_id,event,detail) VALUES(?,?,?,?)",
            (int(time.time()), uid or 0, event, detail or ""),
        )


def touch_user(u):
    uid = u.get("id")
    if not uid:
        return
    now = int(time.time())
    with db() as c:
        c.execute(
            """INSERT INTO users(user_id,username,first_name,joined_at,last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name,
                 last_seen=excluded.last_seen""",
            (uid, u.get("username", ""), u.get("first_name", ""), now, now),
        )


# ------------------------------------------------------------------ হেল্পার
def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_admin(uid):
    return uid in ADMIN_IDS


def channel_id():
    return get_setting("channel") or os.environ.get("CHANNEL", "").strip()


def channel_link():
    link = get_setting("channel_link") or os.environ.get("CHANNEL_LINK", "").strip()
    if link:
        return link
    ch = channel_id()
    if ch.startswith("@") and len(ch) > 1:
        return "https://t.me/" + ch[1:]
    return ""


def storage_id():
    return get_setting("storage") or os.environ.get("STORAGE", "").strip()


def bot_username():
    return get_setting("bot_username", "")


def membership(chat, uid):
    """রিটার্ন: (সদস্য কি না, ত্রুটি)"""
    r = api("getChatMember", chat_id=chat, user_id=uid)
    if not r.ok:
        return False, r.description
    status = r.result.get("status", "")
    if status in ("creator", "administrator", "member"):
        return True, None
    if status == "restricted":
        return bool(r.result.get("is_member")), None
    return False, None


def new_slug(n=10):
    return "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(n))


def deep_link(slug):
    return "https://t.me/%s?start=dl_%s" % (bot_username(), slug)


# ------------------------------------------------------------------ পাঠানো
def send(chat_id, text, kb=None, preview=False):
    params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  disable_web_page_preview=not preview)
    if kb:
        params["reply_markup"] = {"inline_keyboard": kb}
    return api("sendMessage", **params)


def welcome_kb():
    link = channel_link()
    rows = []
    if link:
        rows.append([{"text": "📢 চ্যানেলে জয়েন করুন", "url": link}])
        rows.append([{"text": "✅ জয়েন করেছি", "callback_data": "verify"}])
    return rows or None


def not_joined_text():
    title = esc(get_setting("channel_title") or "আমাদের চ্যানেল")
    return (
        "🔒 <b>ফাইলটি পেতে হলে আগে চ্যানেলে জয়েন করতে হবে</b>\n\n"
        "১. নিচের বাটনে চেপে <b>%s</b> এ জয়েন করুন\n"
        "২. তারপর <b>জয়েন করেছি</b> বাটনে চাপুন\n"
        "৩. সাথে সাথেই ফাইল পেয়ে যাবেন" % title
    )


# ------------------------------------------------------------------ ফাইল ডেলিভারি
def deliver(chat_id, slug, uid):
    with db() as c:
        row = c.execute("SELECT * FROM files WHERE slug=?", (slug,)).fetchone()
    if not row:
        send(chat_id, "❌ এই ফাইলটি আর নেই বা লিংকটি ভুল।")
        return False

    if row["file_id"]:
        r = api(
            "sendDocument",
            chat_id=chat_id,
            document=row["file_id"],
            caption=row["caption"] or "",
            parse_mode="HTML",
        )
        if not r.ok:
            r = api("sendDocument", chat_id=chat_id, document=row["file_id"])
    else:
        r = api(
            "copyMessage",
            chat_id=chat_id,
            from_chat_id=row["chat_id"],
            message_id=row["msg_id"],
        )

    if r.ok:
        with db() as c:
            c.execute("UPDATE users SET downloads=downloads+1 WHERE user_id=?", (uid,))
        log_event(uid, "delivered", slug)
        send(chat_id, "✅ <b>%s</b>\n\nভালো লাগলে বন্ধুদের সাথে শেয়ার করুন।" % esc(row["title"]))
        return True

    send(chat_id, "⚠️ ফাইল পাঠাতে সমস্যা হচ্ছে: %s" % esc(r.description or "unknown"))
    return False


def gate_and_deliver(chat_id, slug, uid):
    ch = channel_id()
    if not ch:
        send(chat_id, "⚙️ বট এখনো সেটআপ হয়নি। অ্যাডমিনকে জানান।")
        return
    ok, err = membership(ch, uid)
    if ok:
        deliver(chat_id, slug, uid)
        return
    if err:
        log.warning("membership check failed: %s", err)
    send(chat_id, not_joined_text(), welcome_kb())


# ------------------------------------------------------------------ হ্যান্ডলার
def on_start(msg, payload):
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    if payload.startswith("dl_"):
        gate_and_deliver(chat_id, payload[3:], uid)
        return
    name = esc(msg["from"].get("first_name") or "বন্ধু")
    text = (
        "👋 স্বাগতম <b>%s</b>!\n\n"
        "এখান থেকে ফাইল ও লিংক পাবেন। প্রতিটি ফাইলের জন্য আলাদা লিংক দেওয়া হয়, "
        "আর লিংকটি কাজ করবে শুধু চ্যানেলে জয়েন করা থাকলে।\n\n"
        "কোনো কাজে সমস্যা হলে /help" % name
    )
    ch = channel_id()
    if ch:
        ok, _ = membership(ch, uid)
        if ok:
            send(chat_id, text + "\n\n✅ আপনি ইতিমধ্যে চ্যানেলে জয়েন করা আছেন।")
            return
    send(chat_id, text, welcome_kb())


HELP = (
    "📖 <b>কমান্ড</b>\n\n"
    "ফাইল পেতে: চ্যানেলের পোস্টে থাকা ডাউনলোড বাটনে চাপুন\n"
    "সাহায্য: /help\n"
    "আপনার আইডি: /id\n\n"
    "<b>অ্যাডমিন কমান্ড</b>\n"
    "/setchannel @yourchannel — গেট চ্যানেল সেট\n"
    "/setlink https://t.me/yourchannel — জয়েন লিংক\n"
    "/storage @yourstorage — ফাইল জমা রাখার চ্যানেল\n"
    "ফাইল পাঠিয়ে ক্যাপশনে লিখুন: <code>/add slug | শিরোনাম</code>\n"
    "/list — সব ফাইল\n"
    "/del slug — ফাইল মুছুন\n"
    "/post slug — চ্যানেলে ডাউনলোড পোস্ট করুন\n"
    "/stats — হিসাব"
)


def on_add(msg):
    """ফাইল সহ মেসেজ: ক্যাপশন /add slug | title"""
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    if not is_admin(uid):
        send(chat_id, "এই কাজ শুধু অ্যাডমিন করতে পারেন।")
        return

    caption = msg.get("caption") or ""
    parts = [p.strip() for p in caption.split("|")]
    head = parts[0]
    arg = head[4:].strip() if head.lower().startswith("/add") else ""
    slug = arg or new_slug()
    title = parts[1] if len(parts) > 1 else slug
    extra = " | ".join(parts[2:]) if len(parts) > 2 else ""

    kind, file_id, msg_chat, msg_id = None, None, None, None
    for key in ("document", "video", "audio", "photo", "animation"):
        if key in msg:
            val = msg[key]
            if isinstance(val, list):
                val = val[-1]
            kind = key
            file_id = val.get("file_id")
            break
    if not file_id:
        send(chat_id, "কোনো ফাইল পাচ্ছি না। ফাইল/ভিডিও/ছবি পাঠিয়ে ক্যাপশনে দিয়ে লিখুন:\n<code>/add slug | শিরোনাম | বর্ণনা</code>")
        return

    store = storage_id()
    if store:
        r = api("copyMessage", chat_id=store, from_chat_id=chat_id, message_id=msg["message_id"])
        if r.ok:
            msg_chat, msg_id = store, r.result.get("message_id")
        else:
            send(chat_id, "⚠️ স্টোরেজ চ্যানেলে কপি হলো না (%s)। বট কি ওখানে অ্যাডমিন?" % esc(r.description or ""))
    if not msg_chat:
        msg_chat, msg_id = chat_id, msg["message_id"]

    body = esc(extra) if extra else ""
    with db() as c:
        c.execute(
            """INSERT INTO files(slug,title,kind,file_id,chat_id,msg_id,caption,created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 title=excluded.title, kind=excluded.kind, file_id=excluded.file_id,
                 chat_id=excluded.chat_id, msg_id=excluded.msg_id, caption=excluded.caption""",
            (slug, title, kind, file_id, str(msg_chat), msg_id, body, int(time.time())),
        )
    log_event(uid, "added", slug)

    send(
        chat_id,
        "✅ সেভ হয়েছে: <b>%s</b>\n\nগেটেড লিংক:\n<code>%s</code>\n\nচ্যানেলে পোস্ট করতে: <code>/post %s</code>"
        % (esc(title), deep_link(slug), slug),
        preview=True,
    )


def on_post(chat_id, slug):
    ch = channel_id()
    if not ch:
        send(chat_id, "আগে /setchannel দিয়ে চ্যানেল সেট করুন।")
        return
    with db() as c:
        row = c.execute("SELECT * FROM files WHERE slug=?", (slug,)).fetchone()
    if not row:
        send(chat_id, "এই slug নেই: %s" % esc(slug))
        return
    kb = [[{"text": "⬇️ ডাউনলোড করুন", "url": deep_link(slug)}]]
    text = "🎁 <b>%s</b>\n\n%s" % (esc(row["title"]), row["caption"] or "")
    r = api("sendMessage", chat_id=ch, text=text, parse_mode="HTML",
            reply_markup={"inline_keyboard": kb})
    if r.ok:
        send(chat_id, "✅ চ্যানেলে পোস্ট হয়েছে (message_id %s)।" % r.result.get("message_id"))
    else:
        send(chat_id, "⚠️ চ্যানেলে পোস্ট হলো না: %s\nবট কি চ্যানেলে অ্যাডমিন?" % esc(r.description or ""))


def on_setchannel(chat_id, arg):
    if not arg:
        send(chat_id, "ব্যবহার: <code>/setchannel @yourchannel</code>")
        return
    r = api("getChat", chat_id=arg)
    if not r.ok:
        send(chat_id, "❌ চ্যানেল পাওয়া যায়নি: %s\nবটকে আগে চ্যানেলে অ্যাডমিন করুন।" % esc(r.description or ""))
        return
    set_setting("channel", arg)
    set_setting("channel_title", r.result.get("title") or arg)
    if r.result.get("username"):
        set_setting("channel_link", "https://t.me/" + r.result["username"])
    me = api("getChatMember", chat_id=arg, user_id=int(get_setting("bot_id", "0") or 0)) if get_setting("bot_id") else None
    line = ""
    if me and not me.ok:
        line = "\n\n⚠️ বটকে এখনো চ্যানেলে অ্যাডমিন করা হয়নি, তাই মেম্বার যাচাই কাজ করবে না।"
    send(chat_id, "✅ গেট চ্যানেল: <b>%s</b> (%s)%s" % (esc(r.result.get("title") or ""), esc(arg), line))


def on_stats(chat_id):
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        files = c.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        dl = c.execute("SELECT COALESCE(SUM(downloads),0) n FROM users").fetchone()["n"]
        today = c.execute(
            "SELECT COUNT(*) n FROM events WHERE event='delivered' AND ts>?",
            (int(time.time()) - 86400,),
        ).fetchone()["n"]
    send(
        chat_id,
        "📊 <b>হিসাব</b>\n\nইউজার: %s\nফাইল: %s\nমোট ডাউনলোড: %s\nশেষ ২৪ ঘণ্টায়: %s" % (total, files, dl, today),
    )


# ------------------------------------------------------------------ মূল লুপ
BUCKET = {}


def throttled(uid):
    now = time.time()
    hits = [t for t in BUCKET.get(uid, []) if now - t < RATE_WINDOW]
    hits.append(now)
    BUCKET[uid] = hits
    return len(hits) > RATE_LIMIT


def handle(upd):
    if "callback_query" in upd:
        cq = upd["callback_query"]
        uid = cq["from"]["id"]
        chat_id = cq["message"]["chat"]["id"]
        api("answerCallbackQuery", callback_query_id=cq["id"])
        if cq.get("data") == "verify":
            ch = channel_id()
            if not ch:
                send(chat_id, "⚙️ বট সেটআপ হয়নি।")
                return
            ok, _ = membership(ch, uid)
            if ok:
                log_event(uid, "verified", "")
                send(chat_id, "✅ ধন্যবাদ! এখন আপনি সব ফাইল পেতে পারবেন।\nচ্যানেলের পোস্টে ডাউনলোড বাটনে চাপ দিন।")
            else:
                send(chat_id, "❌ এখনো জয়েন করা হয়নি। জয়েন করে আবার চেষ্টা করুন।", welcome_kb())
        return

    msg = upd.get("message") or upd.get("channel_post")
    if not msg or "from" not in msg:
        return
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]
    touch_user(msg["from"])

    if throttled(uid):
        return

    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()

    if caption.lower().startswith("/add"):
        on_add(msg)
        return

    if not text:
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd == "/start":
        on_start(msg, arg)
    elif cmd in ("/help", "help"):
        send(chat_id, HELP)
    elif cmd == "/id":
        send(chat_id, "আপনার আইডি: <code>%s</code>\nএই চ্যাট: <code>%s</code>" % (uid, chat_id))
    elif cmd == "/setchannel":
        if is_admin(uid):
            on_setchannel(chat_id, arg)
        else:
            send(chat_id, "শুধু অ্যাডমিন।")
    elif cmd == "/setlink":
        if is_admin(uid) and arg:
            set_setting("channel_link", arg)
            send(chat_id, "✅ জয়েন লিংক সেট হয়েছে।")
        else:
            send(chat_id, "ব্যবহার: <code>/setlink https://t.me/+invite</code>")
    elif cmd == "/storage":
        if is_admin(uid) and arg:
            set_setting("storage", arg)
            send(chat_id, "✅ স্টোরেজ চ্যানেল সেট হয়েছে: %s" % esc(arg))
        else:
            send(chat_id, "ব্যবহার: <code>/storage @yourstorage</code>")
    elif cmd == "/list":
        if not is_admin(uid):
            return
        with db() as c:
            rows = c.execute("SELECT slug,title FROM files ORDER BY created_at DESC LIMIT 30").fetchall()
        if not rows:
            send(chat_id, "এখনো কোনো ফাইল নেই।")
            return
        body = "\n".join("• <code>%s</code> — %s" % (r["slug"], esc(r["title"])) for r in rows)
        send(chat_id, "📁 <b>ফাইল তালিকা</b>\n\n" + body)
    elif cmd == "/del":
        if is_admin(uid) and arg:
            with db() as c:
                c.execute("DELETE FROM files WHERE slug=?", (arg,))
            send(chat_id, "🗑 মুছে ফেলা হয়েছে: %s" % esc(arg))
    elif cmd == "/post":
        if is_admin(uid) and arg:
            on_post(chat_id, arg)
    elif cmd == "/stats":
        if is_admin(uid):
            on_stats(chat_id)
    else:
        if is_admin(uid):
            send(chat_id, "কমান্ড বুঝিনি। /help দেখুন।")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")
    init_db()
    me = api("getMe")
    if not me.ok:
        raise SystemExit("bad token: %s" % me.description)
    set_setting("bot_username", me.result["username"])
    set_setting("bot_id", str(me.result["id"]))
    log.info("bot @%s started, admins=%s", me.result["username"], ADMIN_IDS)

    offset = int(get_setting("offset", "0") or 0)
    while True:
        r = api("getUpdates", offset=offset, timeout=40,
                allowed_updates=["message", "callback_query"])
        if not r.ok:
            time.sleep(3)
            continue
        for upd in r.result:
            offset = upd["update_id"] + 1
            try:
                handle(upd)
            except Exception as e:
                log.exception("handler error: %s", e)
        if r.result:
            set_setting("offset", str(offset))


if __name__ == "__main__":
    main()
