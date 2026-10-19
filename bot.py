import os
import asyncio
import json
import logging
import time

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, constants
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest, GetFullChatRequest
)
from telethon.tl.functions.phone import (
    LeaveGroupCallRequest, EditGroupCallParticipantRequest
)
from telethon.tl.types import (
    InputPeerSelf, UpdateGroupCallParticipants,
    Channel, Chat as TelegramChat
)

# ─── REAL UDP CALL ENGINE ────────────────────────────────────────────────────────
# আসল human app যেমন Telegram-এর WebRTC (UDP) দিয়ে call-এ থাকে,
# ঠিক সেভাবেই ntgcalls দিয়ে প্রতিটি account আসল media connection রাখবে
from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
OWNER_ID      = int(os.environ.get("OWNER_ID", "0"))
API_ID        = int(os.environ.get("API_ID", "0"))
API_HASH      = os.environ.get("API_HASH", "")
ACCOUNTS_FILE = "accounts.json"

# Live-তে auto-leave ঠেকাতে keep-alive ping interval (second)
KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL", "4"))

# ─── STATES ────────────────────────────────────────────────────────────────────
MAIN_MENU, WAIT_PHONE, WAIT_CODE, WAIT_2FA, WAIT_LINK, WAIT_COUNT = range(6)

# ─── STORAGE ───────────────────────────────────────────────────────────────────
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# In-memory
pending_clients = {}  # phone → client (login চলাকালীন)
active_clients  = {}  # phone → client (ready)
live_sessions   = {}  # bot chat_id → session info
live_handlers   = {}  # phone → [handler functions]
call_engines    = {}  # phone → PyTgCalls (real UDP/WebRTC engine)
watchdog_tasks  = {}  # phone → asyncio.Task (4-sec check + auto-rejoin)
live_state      = {}  # phone → {engine, chat_id, call, client, me}

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"), KeyboardButton("📋 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗜𝗦𝗧")],
        [KeyboardButton("🟢 𝗝𝗢𝗜𝗡 𝗢𝗡 𝗟𝗜𝗩𝗘")],
    ], resize_keyboard=True)

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

# ─── ADMIN SYSTEM (owner নিজে admin দিতে পারবেন /add, বাদ দিতে পারবেন /rm) ───
ADMIN_FILE = "admins.json"

def load_admins():
    if os.path.exists(ADMIN_FILE):
        try:
            with open(ADMIN_FILE) as f:
                return set(int(x) for x in json.load(f))
        except Exception:
            return set()
    return set()

def save_admins(ids):
    with open(ADMIN_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)

admins = load_admins()   # persistent: restart-এও থাকবে

def is_admin_user(uid: int) -> bool:
    return uid == OWNER_ID or uid in admins

def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and is_admin_user(u.id))

async def get_client(phone: str, session_str: str) -> TelegramClient:
    if phone in active_clients:
        c = active_clients[phone]
        if not c.is_connected():
            await c.connect()
        return c
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await c.connect()
    active_clients[phone] = c
    return c

# ─── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Only owner/admin allowed.")
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 *Welcome to Account Manager Bot!*\n\nChoose an option 👇",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )
    return MAIN_MENU

# ─── MAIN MENU ROUTER ──────────────────────────────────────────────────────────
async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    t = update.message.text or ""
    if "➕" in t:
        return await add_start(update, ctx)
    elif "📋" in t:
        return await show_list(update, ctx)
    elif "🟢" in t:
        return await live_start(update, ctx)
    await update.message.reply_text("Please use the buttons 👇", reply_markup=main_kb())
    return MAIN_MENU

# ─── ADD ACCOUNT ───────────────────────────────────────────────────────────────
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = len(load_accounts())
    await update.message.reply_text(
        f"*𝗔𝗟𝗥𝗘𝗔𝗗𝗬 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 : {n}*\n\n"
        "নতুন একাউন্ট এর নাম্বার দিন 👇\n\n"
        "_Example : +8801XXXXXXXXX_",
        parse_mode="Markdown"
    )
    return WAIT_PHONE

async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    phone = update.message.text.strip().replace(" ", "")
    ctx.user_data["phone"] = phone

    if phone in load_accounts():
        await update.message.reply_text("⚠️ এই নাম্বার ইতিমধ্যে আছে!", reply_markup=main_kb())
        return MAIN_MENU

    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect()
    pending_clients[phone] = c
    try:
        r = await c.send_code_request(phone)
        ctx.user_data["code_hash"] = r.phone_code_hash
        await update.message.reply_text("✅ কোড পাঠানো হয়েছে!\n\n*ভেরিফিকেশন কোড দিন* 👇", parse_mode="Markdown")
        return WAIT_CODE
    except FloodWaitError as e:
        await update.message.reply_text(f"⏳ {e.seconds} সেকেন্ড পরে চেষ্টা করুন।", reply_markup=main_kb())
        await c.disconnect(); del pending_clients[phone]
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_kb())
        await c.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU

async def got_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    phone  = ctx.user_data.get("phone")
    client = pending_clients.get(phone)
    if not client:
        await update.message.reply_text("❌ Session শেষ। আবার /start করুন।", reply_markup=main_kb())
        return MAIN_MENU
    try:
        await client.sign_in(phone, update.message.text.strip(),
                             phone_code_hash=ctx.user_data["code_hash"])
        return await _save_account(update, ctx, client, phone)
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 *2FA পাসওয়ার্ড দিন* 👇", parse_mode="Markdown")
        return WAIT_2FA
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await update.message.reply_text("❌ কোড ভুল বা মেয়াদ শেষ।", reply_markup=main_kb())
        await client.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_kb())
        await client.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU

async def got_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    phone  = ctx.user_data.get("phone")
    client = pending_clients.get(phone)
    if not client:
        await update.message.reply_text("❌ Session শেষ।", reply_markup=main_kb())
        return MAIN_MENU
    try:
        await client.sign_in(password=update.message.text.strip())
        return await _save_account(update, ctx, client, phone)
    except Exception as e:
        await update.message.reply_text(f"❌ ভুল পাসওয়ার্ড: {e}", reply_markup=main_kb())
        await client.disconnect()
        if phone in pending_clients: del pending_clients[phone]
        return MAIN_MENU

async def _save_account(update, ctx, client, phone):
    me   = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
    accs = load_accounts()
    accs[phone] = {"name": name, "session": client.session.save()}
    save_accounts(accs)
    active_clients[phone] = client
    if phone in pending_clients: del pending_clients[phone]
    await update.message.reply_text(
        f"✅ *Account Login Success!*\n\n👤 `{name}`\n📱 `{phone}`",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    return MAIN_MENU

# ─── ACCOUNT LIST ──────────────────────────────────────────────────────────────
async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accs = load_accounts()
    if not accs:
        await update.message.reply_text("📋 কোনো অ্যাকাউন্ট নেই।", reply_markup=main_kb())
        return MAIN_MENU
    lines = [f"*𝗧𝗼𝘁𝗮𝗹 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗖𝗼𝘂𝗻𝘁 : {len(accs)}*\n"]
    for i, (ph, info) in enumerate(accs.items(), 1):
        lines.append(f"{i}. 👤 `{info['name']}` — `{ph}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())
    return MAIN_MENU

# ─── LIVE JOIN ─────────────────────────────────────────────────────────────────
async def live_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not load_accounts():
        await update.message.reply_text("❌ আগে অ্যাকাউন্ট যোগ করুন।", reply_markup=main_kb())
        return MAIN_MENU
    await update.message.reply_text(
        "🎥 *Live link বা @username দিন* 👇\n\n"
        "_Public : @channelname বা t.me/channelname_\n"
        "_Private : t.me/+xxxxxxxx_",
        parse_mode="Markdown"
    )
    return WAIT_LINK

async def got_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    link = update.message.text.strip()
    ctx.user_data["live_link"] = link
    accs = load_accounts()
    total = len(accs)

    if total == 1:
        ctx.user_data["join_count"] = 1
        return await _do_join(update, ctx)

    await update.message.reply_text(
        f"👥 মোট *{total}টি* অ্যাকাউন্ট আছে।\n"
        f"কতটি দিয়ে Join করাবেন? *(1 - {total})*",
        parse_mode="Markdown"
    )
    return WAIT_COUNT

async def got_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return ConversationHandler.END
    accs = load_accounts()
    try:
        n = int(update.message.text.strip())
        if not 1 <= n <= len(accs): raise ValueError
    except ValueError:
        await update.message.reply_text(f"⚠️ সঠিক সংখ্যা দিন (1 - {len(accs)})")
        return WAIT_COUNT
    ctx.user_data["join_count"] = n
    return await _do_join(update, ctx)

async def _do_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    link    = ctx.user_data["live_link"]
    count   = ctx.user_data["join_count"]
    accs    = load_accounts()
    phones  = list(accs.keys())[:count]
    chat_id = update.effective_chat.id

    msg = await update.message.reply_text(
        f"⏳ *{count}টি* অ্যাকাউন্ট Join হচ্ছে...",
        parse_mode="Markdown"
    )

    joined    = []
    calls_map = {}
    errors    = []

    for phone in phones:
        info = accs.get(phone)
        if not info: continue
        try:
            client = await get_client(phone, info["session"])

            # ── Step 1: Resolve entity & join channel ──────────────────
            entity = await _resolve_and_join(client, link)
            if entity is None:
                logging.warning(f"[{phone}] Could not resolve entity for {link}")
                errors.append("Link/invite resolve হয়নি (link ভুল অথবা access নেই)")
                continue

            # ── Step 2: Get active group call ──────────────────────────
            # Channel vs Group এর জন্য আলাদা request
            try:
                if isinstance(entity, Channel):
                    full = await client(GetFullChannelRequest(entity))
                else:
                    # Regular group/supergroup
                    full = await client(GetFullChatRequest(entity.id))
                call = getattr(full.full_chat, 'call', None)
            except Exception as e:
                logging.warning(f"[{phone}] Could not get full chat: {e}")
                # Fallback: try the other request type
                try:
                    full = await client(GetFullChatRequest(entity.id))
                    call = getattr(full.full_chat, 'call', None)
                except Exception:
                    call = None

            if call is None:
                logging.warning(f"[{phone}] No active voice/live chat at {link}")
                errors.append(
                    "এখন কোনো চলমান live/voice chat নেই "
                    "(live শেষ হয়ে গেছে অথবা scheduled আছে) — "
                    "আগে live চালু করে তারপর join দিন"
                )
                continue

            # ── Step 3: Real UDP/WebRTC দিয়ে live join (যেমন আসল human) ─
            me     = await client.get_me()
            engine = await _get_engine(phone, client)
            vchat = get_peer_id(entity)

            await _engine_join(phone, engine, vchat, call, me, client)

            # ── Step 4: Register auto-remute + kick-detect handler ──────
            handler = _make_remute_handler(client, call, me.id, phone)
            client.add_event_handler(handler, events.Raw(UpdateGroupCallParticipants))
            live_handlers.setdefault(phone, []).append(handler)

            # ── Step 5: Watchdog (প্রতি 4 সেকেন্ড চেক, drop হলে ফিরে যাবে) ─
            state = {
                "engine":  engine,
                "chat_id": vchat,
                "call":    call,
                "client":  client,
                "me":      me,
            }
            live_state[phone] = state
            _start_watchdog(phone, state)

            calls_map[phone] = call
            joined.append(phone)
            logging.info(f"[{phone}] ✅ Joined live via REAL UDP engine")

        except NoActiveGroupCall:
            logging.error(f"[{phone}] NoActiveGroupCall (live চালু নেই)")
            errors.append(f"{phone}: Live call পাওয়া যায়নি — লাইভ চালু আছে কিনা দেখুন")
        except Exception as e:
            logging.exception(f"[{phone}] Live join failed")
            errors.append(f"{phone}: {type(e).__name__}: {e}")

    if not joined:
        detail = "\n".join(f"• {x}" for x in dict.fromkeys(errors)) or "• অজানা কারণ"
        await msg.edit_text(
            "❌ Join করা সম্ভব হয়নি।\n\n🔎 আসল কারণ:\n" + detail,
            reply_markup=InlineKeyboardMarkup([[]])
        )
        await update.message.reply_text("Main menu 👇", reply_markup=main_kb())
        return MAIN_MENU

    live_sessions[chat_id] = {
        "phones": joined,
        "calls":  calls_map,
        "link":   link,
    }

    await msg.edit_text(
        f"✅ *YOUR ACCOUNT LIVE JOIN SUCCESS* 🎉\n\n"
        f"✔️ Joined : *{len(joined)}* accounts\n"
        f"❌ Failed : *{count - len(joined)}* accounts\n\n"
        f"📡 Real UDP connection চালু — এখন auto-leave হবে না",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 LIVE LEAVE", callback_data=f"leave:{chat_id}")
        ]])
    )
    if count > len(joined) and errors:
        uniq = list(dict.fromkeys(errors))[:4]
        await update.message.reply_text(
            "⚠️ যেসব account join হয়নি — কারণ:\n" + "\n".join(f"• {x}" for x in uniq)
        )
    return MAIN_MENU

async def _resolve_and_join(client: TelegramClient, link: str):
    """Resolve link → join if needed → return entity (or None)."""
    try:
        if "t.me/+" in link or "joinchat/" in link:
            # ── Private invite link ────────────────────────────────────
            invite_hash = (
                link.split("/+")[-1]     if "/+" in link
                else link.split("joinchat/")[-1]
            ).strip()
            try:
                check = await client(CheckChatInviteRequest(invite_hash))
                # Already a member if 'chat' attribute present
                if hasattr(check, 'chat'):
                    return check.chat
                # Not yet joined
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0] if result.chats else None
            except Exception:
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0] if result.chats else None
        else:
            # ── Public username / link ─────────────────────────────────
            username = (
                link
                .replace("https://", "")
                .replace("http://", "")
                .replace("t.me/", "")
                .replace("@", "")
                .strip()
                .split("/")[0]
            )
            entity = await client.get_entity(username)
            # Join silently if not already a member
            try:
                await client(JoinChannelRequest(entity))
            except Exception:
                pass
            return entity
    except Exception as e:
        logging.error(f"[RESOLVE] {link} → {e}")
        return None

_last_mute_at: dict = {}   # flood-wait এড়াতে: সাম্প্রতিক auto-mute মনে রাখে

def _make_remute_handler(client: TelegramClient, call, me_id: int, phone: str = None):
    """
    - admin unmute করলে সাথে সাথে আবার auto-mute
    - account live থেকে বের হয়ে গেলে (left=True) log করে → watchdog ফিরে ঢুকাবে
    """
    async def handler(ev):
        for p in ev.participants:
            uid = getattr(p.peer, 'user_id', None)
            if uid != me_id:
                continue
            if getattr(p, "left", False):
                logging.warning(f"[KICKED] {phone} live থেকে বের হয়েছে → watchdog ফিরে ঢুকাবে")
                continue
            if not p.muted:
                now = time.monotonic()
                if now - _last_mute_at.get(me_id, 0) < 60:
                    continue   # ১ মিনিটের মধ্যে mute করা হয়েছে — flood-wait এড়াতে
                _last_mute_at[me_id] = now
                try:
                    await client(EditGroupCallParticipantRequest(
                        call=call,
                        participant=InputPeerSelf(),
                        muted=True,
                    ))
                    logging.info(f"[AUTO-MUTE] Re-muted user {me_id}")
                except Exception as ex:
                    logging.warning(f"[AUTO-MUTE] Failed for {me_id}: {ex}")
    return handler

# ─── REAL UDP CALL ENGINE (আসল human-এর মতো live-এ থাকে) ───────────────────────
SILENCE_FILE = "silence.wav"

def _ensure_silence() -> str:
    """Engine-এ আসল media path চালু রাখার জন্য silent audio file (৬০ সেকেন্ড)"""
    try:
        if not os.path.exists(SILENCE_FILE):
            import wave
            with wave.open(SILENCE_FILE, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 8000 * 60)   # ১ মিনিট নীরব audio
    except Exception as e:
        logging.warning(f"[SILENCE] {e}")
    return SILENCE_FILE

def _silence_stream():
    """Raw stream — ffprobe/ffmpeg বাইরে লাগে না, ntgcalls নিজের ভেতরের
    decoder দিয়ে ফাইল পড়ে (MediaSource.FILE)"""
    from pytgcalls.types.raw import Stream, AudioStream, AudioParameters
    from ntgcalls import MediaSource
    return Stream(
        microphone=AudioStream(
            MediaSource.FILE, _ensure_silence(),
            AudioParameters(bitrate=48000, channels=1),
        )
    )

def _tg_filter_stream_end():
    from pytgcalls import filters as tg_filters
    return tg_filters.stream_end()

def _register_stream_loop(engine: PyTgCalls, phone: str):
    """Silent stream শেষ হলে নিজে থেকেই আবার চালু করবে (loop)"""

    @engine.on_update(_tg_filter_stream_end())
    async def _on_stream_end(c, update):
        try:
            await asyncio.sleep(2)   # hot loop এড়াতে
            await c.play(update.chat_id, _silence_stream(),
                         config=GroupCallConfig(auto_start=False))
            logging.info(f"[{phone}] 🔄 silent stream আবার চালু")
        except Exception as e:
            logging.warning(f"[{phone}] stream replay: {e}")

async def _get_engine(phone: str, client: TelegramClient) -> PyTgCalls:
    """প্রতিটি account-এর জন্য একটি করে ntgcalls engine (real WebRTC/UDP)"""
    eng = call_engines.get(phone)
    if eng is None:
        eng = PyTgCalls(client, workers=2)
        call_engines[phone] = eng
        _register_stream_loop(eng, phone)
    if not eng._is_running:
        await eng.start()
    return eng

async def _engine_join(phone: str, engine: PyTgCalls, chat_id: int,
                       call, me, client: TelegramClient):
    """Real UDP/WebRTC connection দিয়ে live-এ ঢোকায় (muted থাকে)"""
    # join_as সবসময় InputPeerUser দিন (full User বা InputPeerSelf দিলে
    # pytgcalls-এর internal chat_id() ভেঙে error আসে) — cache clear হলেও
    # NoneType error আসে না
    try:
        from telethon.tl.types import InputPeerUser
        join_as = InputPeerUser(user_id=me.id, access_hash=me.access_hash)
    except Exception:
        join_as = me
    try:
        # raw stream — ffprobe/ffmpeg লাগে না (ntgcalls নিজে ফাইল পড়ে, loop করে)
        await engine.play(chat_id, _silence_stream(),
                          config=GroupCallConfig(join_as=join_as, auto_start=False))
    except Exception as e:
        logging.warning(f"[{phone}] silent stream failed ({e}) → media ছাড়া join")
        await engine.play(chat_id, None,
                          config=GroupCallConfig(join_as=join_as, auto_start=False))
    try:
        await engine.mute(chat_id)              # মাইক বন্ধ
    except Exception as e:
        logging.warning(f"[{phone}] engine mute: {e}")
    try:
        await client(EditGroupCallParticipantRequest(  # লিস্টেও muted
            call=call, participant=InputPeerSelf(), muted=True))
    except Exception as e:
        logging.warning(f"[{phone}] mtproto mute: {e}")
    logging.info(f"[{phone}] ✅ ঢুকেছে REAL UDP call-এ (muted)")

async def _conn_state(engine: PyTgCalls, chat_id: int):
    """(in_call, info) — engine-এর public API দিয়ে যাচাই: call-এ আছে কিনা।
    (নোট: _binding.get_state() MediaState object ফেরায় — connection state না,
    তাই সেটা দিয়ে disconnected বোঝা যায় না)"""
    try:
        calls = await engine.calls          # {chat_id: Call} — public API
        if chat_id not in calls:
            return False, "NOT_IN_CALL"
        return True, "OK"
    except Exception:
        return True, "UNKNOWN"   # যাচাই করতে না পারলে হস্তক্ষেপ করবে না

async def _watchdog_loop(phone: str, state: dict):
    """
    প্রতি KEEPALIVE_INTERVAL (ডিফল্ট 4) সেকেন্ড পর পর চেক করে —
    connection drop হলে নিজে থেকেই আবার live-এ ঢুকে দেয়।
    """
    checks  = 0
    fails   = 0
    logging.info(f"[WATCHDOG] {phone} 🟢 started (every {KEEPALIVE_INTERVAL}s)")
    while True:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            in_call, st_name = await _conn_state(state["engine"], state["chat_id"])
            if in_call:
                checks += 1
                if checks % 15 == 0:  # ~1 মিনিট পর confirmation
                    logging.info(f"[WATCHDOG] {phone} ✅ live-এ আছে ({checks} checks)")
                continue

            logging.warning(
                f"[WATCHDOG] {phone} 🔄 connection গেছে "
                f"(in_call={in_call}, state={st_name}) → আবার join হচ্ছে"
            )
            if in_call:
                try:
                    await state["engine"].leave_call(state["chat_id"])
                except Exception:
                    pass
                await asyncio.sleep(1)  # callback settle হওয়ার জন্য
            await _engine_join(phone, state["engine"], state["chat_id"],
                               state["call"], state["me"], state["client"])
            fails = 0
        except asyncio.CancelledError:
            logging.info(f"[WATCHDOG] {phone} ⛔ stopped")
            raise
        except NoActiveGroupCall:
            logging.warning(f"[WATCHDOG] {phone} ⛔ live শেষ (call নেই) → watchdog বন্ধ")
            break
        except Exception as e:
            fails += 1
            logging.exception(f"[WATCHDOG] {phone} ⚠️ rejoin failed ({fails}x)")
            # বারবার fail হলে ধীরে ধীরে বেশি অপেক্ষা (flood এড়াতে)
            try:
                await asyncio.sleep(min(KEEPALIVE_INTERVAL * (2 ** fails), 60))
            except asyncio.CancelledError:
                raise

def _start_watchdog(phone: str, state: dict):
    _stop_watchdog(phone)
    watchdog_tasks[phone] = asyncio.create_task(_watchdog_loop(phone, state))

def _stop_watchdog(phone: str):
    task = watchdog_tasks.pop(phone, None)
    if task and not task.done():
        task.cancel()

# ─── LIVE LEAVE CALLBACK ────────────────────────────────────────────────────────
async def live_leave_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin_user(q.from_user.id):
        await q.answer("⛔ Only owner/admin!", show_alert=True)
        return

    chat_id = int(q.data.split(":")[1])
    session = live_sessions.get(chat_id)

    if not session:
        await q.edit_message_text("⚠️ কোনো active session নেই।")
        return

    await q.edit_message_text("⏳ সব অ্যাকাউন্ট Leave হচ্ছে...")

    left = 0
    accs = load_accounts()

    for phone in session["phones"]:
        # আগেই watchdog বন্ধ করো, নাহলে leave করার পরেও আবার ঢুকে দেবে
        _stop_watchdog(phone)
        st     = live_state.pop(phone, {})
        engine = st.get("engine")
        vchat  = st.get("chat_id")
        call   = session["calls"].get(phone)
        info   = accs.get(phone)
        if not info:
            continue
        try:
            client = await get_client(phone, info["session"])
            # Remove auto-remute handlers first
            for h in live_handlers.pop(phone, []):
                try:
                    client.remove_event_handler(h)
                except Exception:
                    pass
            # Leave the call
            if engine is not None and vchat is not None:
                try:
                    await engine.leave_call(vchat)
                except NotInCallError:
                    pass
            elif call:
                await client(LeaveGroupCallRequest(call=call, source=0))
            left += 1
        except Exception as e:
            logging.error(f"[LEAVE] {phone}: {e}")

    del live_sessions[chat_id]
    await q.edit_message_text(
        f"✅ *{left}টি* অ্যাকাউন্ট Live Leave করেছে।",
        parse_mode="Markdown"
    )

# ─── CANCEL ─────────────────────────────────────────────────────────────────────
# ─── ADMIN MANAGEMENT (শুধু owner ব্যবহার করতে পারবেন) ──────────────────────────
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ /add user_id — অন্য কাউকে admin বানাবে (owner-এর মতো বট ব্যবহার করতে পারবে) """
    if not is_owner(update):
        await update.message.reply_text("⛔ এই কমান্ড শুধুই Owner ব্যবহার করতে পারবেন।")
        return
    if not ctx.args:
        await update.message.reply_text(
            "📖 *ব্যবহার:* `/add user_id`\n"
            "উদাহরণ: `/add 123456789`\n\n"
            "user_id জানতে যাকে admin দিতে চান, তাকে @userinfobot এ "
            "পাঠান — সেখান থেকে আইডি নিন।",
            parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id হতে হবে সংখ্যা।")
        return
    if uid == OWNER_ID:
        await update.message.reply_text("⚠️ এটা আপনার নিজের owner ID — এটা আগে থেকেই সবকিছু করতে পারে।")
        return
    if uid in admins:
        await update.message.reply_text(f"⚠️ `{uid}` আগে থেকেই admin।", parse_mode="Markdown")
        return
    admins.add(uid)
    save_admins(admins)
    await update.message.reply_text(
        f"✅ *Admin যোগ হয়েছে:* `{uid}`\n\n"
        "সে এখন আপনার মতোই বটের সব feature (account add, live join/leave) "
        "ব্যবহার করতে পারবে। /start দিয়ে শুরু করবে।",
        parse_mode="Markdown")

async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ /rm user_id — admin-ship ফিরিয়ে নেবে """
    if not is_owner(update):
        await update.message.reply_text("⛔ এই কমান্ড শুধুই Owner ব্যবহার করতে পারবেন।")
        return
    if not ctx.args:
        admins_list = "\n".join(f"• `{a}`" for a in sorted(admins)) or "(কোনো admin নেই)"
        await update.message.reply_text(
            "📖 *ব্যবহার:* `/rm user_id`\n\n"
            "*বর্তমান admins:*\n" + admins_list,
            parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id হতে হবে সংখ্যা।")
        return
    if uid not in admins:
        await update.message.reply_text(f"⚠️ `{uid}` এখন admin না।", parse_mode="Markdown")
        return
    admins.discard(uid)
    save_admins(admins)
    await update.message.reply_text(f"🗑️ `{uid}` এর admin-ship নেমে গেছে।", parse_mode="Markdown")

async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ /admins — বর্তমান admin তালিকা দেখাবে (owner ও admin দেখতে পারবে) """
    if not is_admin(update):
        return
    owners = f"👑 Owner: `{OWNER_ID}`\n" if OWNER_ID else "👑 Owner: (set হয়নি)\n"
    admins_list = "\n".join(f"• `{a}`" for a in sorted(admins)) or "(কোনো admin নেই)"
    await update.message.reply_text(
        owners + "\n*Admins:*\n" + admins_list,
        parse_mode="Markdown")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ বাতিল।", reply_markup=main_kb())
    return MAIN_MENU

# ─── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU:  [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)],
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            WAIT_CODE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_code)],
            WAIT_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_2fa)],
            WAIT_LINK:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_link)],
            WAIT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_count)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(live_leave_cb, pattern=r"^leave:"))
    app.add_handler(CommandHandler("add", cmd_add))       # শুধু owner
    app.add_handler(CommandHandler("rm", cmd_rm))         # শুধু owner
    app.add_handler(CommandHandler("admins", cmd_admins))

    print("🤖 Bot starting...")
    async with app:
        await app.initialize()
        # পুরনো webhook ও pending updates পরিষ্কার করো
        await app.bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(1)  # পুরনো instance বন্ধ হওয়ার জন্য অপেক্ষা
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        print("✅ Bot is running! Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        finally:
            # বন্ধ হওয়ার সময় সব watchdog task বন্ধ
            for ph in list(watchdog_tasks):
                _stop_watchdog(ph)
            live_state.clear()

if __name__ == "__main__":
    asyncio.run(main())
