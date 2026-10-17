import os
import asyncio
import json
import logging
import subprocess

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest
)

from pytgcalls import PyTgCalls
from pytgcalls.types import Update as CallUpdate, StreamType
from pytgcalls.types.input_stream import AudioPiped
from pytgcalls.types.input_stream.quality import AudioQuality

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
SILENT_AUDIO  = "silent.raw"   # ffmpeg দিয়ে তৈরি করা হবে

# ─── STATES ────────────────────────────────────────────────────────────────────
MAIN_MENU, WAIT_PHONE, WAIT_CODE, WAIT_2FA, WAIT_LINK, WAIT_COUNT = range(6)

# ─── IN-MEMORY STORAGE ─────────────────────────────────────────────────────────
pending_clients = {}   # phone → TelegramClient (login চলাকালীন)
active_clients  = {}   # phone → TelegramClient (ready)
call_instances  = {}   # phone → PyTgCalls instance
live_sessions   = {}   # bot_chat_id → {phones, chat_peer, link}

# ─── SILENT AUDIO GENERATION ───────────────────────────────────────────────────
def ensure_silent_audio():
    """
    একটা silent raw PCM file তৈরি করে।
    pytgcalls এটাকে stream করে — real UDP connection open হয়,
    কিন্তু কোনো sound নেই। Telegram মনে করে real user আছে।
    """
    if not os.path.exists(SILENT_AUDIO):
        logging.info("[AUDIO] Generating silent audio file...")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", "7200",          # 2 ঘণ্টার silent audio
            "-f", "s16le",         # raw PCM format
            "-acodec", "pcm_s16le",
            SILENT_AUDIO
        ], capture_output=True)
        logging.info("[AUDIO] Silent audio ready.")

# ─── STORAGE ───────────────────────────────────────────────────────────────────
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"), KeyboardButton("📋 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗜𝗦𝗧")],
        [KeyboardButton("🟢 𝗝𝗢𝗜𝗡 𝗢𝗡 𝗟𝗜𝗩𝗘")],
    ], resize_keyboard=True)

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

async def get_telethon_client(phone: str, session_str: str) -> TelegramClient:
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
    if not is_owner(update):
        await update.message.reply_text("⛔ Only owner allowed.")
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 *Welcome to Account Manager Bot!*\n\nChoose an option 👇",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )
    return MAIN_MENU

# ─── MAIN MENU ROUTER ──────────────────────────────────────────────────────────
async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
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
    if not is_owner(update): return ConversationHandler.END
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
    if not is_owner(update): return ConversationHandler.END
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
    if not is_owner(update): return ConversationHandler.END
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
    if not is_owner(update): return ConversationHandler.END
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
    if not is_owner(update): return ConversationHandler.END
    accs = load_accounts()
    try:
        n = int(update.message.text.strip())
        if not 1 <= n <= len(accs): raise ValueError
    except ValueError:
        await update.message.reply_text(f"⚠️ সঠিক সংখ্যা দিন (1 - {len(accs)})")
        return WAIT_COUNT
    ctx.user_data["join_count"] = n
    return await _do_join(update, ctx)

async def _resolve_and_join(client: TelegramClient, link: str):
    """Channel resolve করো এবং join করো, chat peer return করো।"""
    try:
        if "t.me/+" in link or "joinchat/" in link:
            invite_hash = (
                link.split("/+")[-1] if "/+" in link
                else link.split("joinchat/")[-1]
            ).strip()
            try:
                check = await client(CheckChatInviteRequest(invite_hash))
                if hasattr(check, 'chat'):
                    return check.chat
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0] if result.chats else None
            except Exception:
                result = await client(ImportChatInviteRequest(invite_hash))
                return result.chats[0] if result.chats else None
        else:
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
            try:
                await client(JoinChannelRequest(entity))
            except Exception:
                pass
            return entity
    except Exception as e:
        logging.error(f"[RESOLVE] {link} → {e}")
        return None

async def _do_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    link    = ctx.user_data["live_link"]
    count   = ctx.user_data["join_count"]
    accs    = load_accounts()
    phones  = list(accs.keys())[:count]
    bot_chat_id = update.effective_chat.id

    msg = await update.message.reply_text(
        f"⏳ *{count}টি* অ্যাকাউন্ট Join হচ্ছে...",
        parse_mode="Markdown"
    )

    joined     = []
    chat_peers = {}

    for phone in phones:
        info = accs.get(phone)
        if not info:
            continue
        try:
            # ── Step 1: Telethon client নাও ──────────────────────────
            telethon_client = await get_telethon_client(phone, info["session"])

            # ── Step 2: Channel resolve ও join ───────────────────────
            entity = await _resolve_and_join(telethon_client, link)
            if entity is None:
                logging.warning(f"[{phone}] Could not resolve entity")
                continue

            # chat_id বের করো
            chat_id = entity.id

            # ── Step 3: PyTgCalls instance তৈরি ──────────────────────
            # প্রতিটা account-এর জন্য আলাদা PyTgCalls instance
            if phone not in call_instances:
                call_py = PyTgCalls(telethon_client)
                await call_py.start()
                call_instances[phone] = call_py
            else:
                call_py = call_instances[phone]

            # ── Step 4: Silent audio stream দিয়ে join ────────────────
            # এটাই real UDP connection তৈরি করে
            # Telegram server মনে করে genuine audio client আছে
            await call_py.join_group_call(
                chat_id,
                AudioPiped(
                    SILENT_AUDIO,
                    AudioQuality.STUDIO,
                ),
                stream_type=StreamType().pulse_stream,
            )

            chat_peers[phone] = chat_id
            joined.append(phone)
            logging.info(f"[{phone}] ✅ Joined live via PyTgCalls (real UDP)")

        except Exception as e:
            logging.error(f"[{phone}] Live join failed: {e}")

    if not joined:
        await msg.edit_text(
            "❌ *Join করা সম্ভব হয়নি।*\n\n"
            "সম্ভাব্য কারণ:\n"
            "• Channel এ Active voice/live chat নেই\n"
            "• Link ভুল অথবা access নেই\n"
            "• ffmpeg ইনস্টল নেই",
            parse_mode="Markdown"
        )
        await update.message.reply_text("Main menu 👇", reply_markup=main_kb())
        return MAIN_MENU

    live_sessions[bot_chat_id] = {
        "phones":     joined,
        "chat_peers": chat_peers,
        "link":       link,
    }

    await msg.edit_text(
        f"✅ *YOUR ACCOUNT LIVE JOIN SUCCESS* 🎉\n\n"
        f"✔️ Joined : *{len(joined)}* accounts\n"
        f"❌ Failed : *{count - len(joined)}* accounts\n\n"
        f"🔵 Real UDP stream active — always থাকবে",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 LIVE LEAVE", callback_data=f"leave:{bot_chat_id}")
        ]])
    )
    return MAIN_MENU

# ─── LIVE LEAVE CALLBACK ────────────────────────────────────────────────────────
async def live_leave_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.from_user.id != OWNER_ID:
        await q.answer("⛔ Only owner!", show_alert=True)
        return

    bot_chat_id = int(q.data.split(":")[1])
    session     = live_sessions.get(bot_chat_id)

    if not session:
        await q.edit_message_text("⚠️ কোনো active session নেই।")
        return

    await q.edit_message_text("⏳ সব অ্যাকাউন্ট Leave হচ্ছে...")

    left = 0
    for phone in session["phones"]:
        chat_id = session["chat_peers"].get(phone)
        call_py = call_instances.get(phone)
        if not call_py or not chat_id:
            continue
        try:
            await call_py.leave_group_call(chat_id)
            left += 1
            logging.info(f"[{phone}] ✅ Left live")
        except Exception as e:
            logging.error(f"[LEAVE] {phone}: {e}")

    del live_sessions[bot_chat_id]

    await q.edit_message_text(
        f"✅ *{left}টি* অ্যাকাউন্ট Live Leave করেছে।",
        parse_mode="Markdown"
    )

# ─── CANCEL ─────────────────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ বাতিল।", reply_markup=main_kb())
    return MAIN_MENU

# ─── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    # Silent audio file তৈরি করো (একবারই করবে)
    ensure_silent_audio()

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

    print("🤖 Bot starting...")
    async with app:
        await app.initialize()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await asyncio.sleep(1)
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        print("✅ Bot is running! Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()
        finally:
            # সব PyTgCalls instance বন্ধ করো
            for phone, call_py in list(call_instances.items()):
                try:
                    await call_py.stop()
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(main())
