import os
import asyncio
import json
import logging
import random

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
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest, GetFullChatRequest
)
from telethon.tl.functions.phone import (
    JoinGroupCallRequest, LeaveGroupCallRequest, EditGroupCallParticipantRequest
)
from telethon.tl.types import (
    DataJSON, InputPeerSelf, UpdateGroupCallParticipants,
    Channel, Chat as TelegramChat
)

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
live_sessions   = {}  # chat_id → session info
live_handlers   = {}  # phone → [handler functions]

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"), KeyboardButton("📋 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗜𝗦𝗧")],
        [KeyboardButton("🟢 𝗝𝗢𝗜𝗡 𝗢𝗡 𝗟𝗜𝗩𝗘")],
    ], resize_keyboard=True)

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

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
    ssrc_map  = {}

    for phone in phones:
        info = accs.get(phone)
        if not info: continue
        try:
            client = await get_client(phone, info["session"])

            # ── Step 1: Resolve entity & join channel ──────────────────
            entity = await _resolve_and_join(client, link)
            if entity is None:
                logging.warning(f"[{phone}] Could not resolve entity for {link}")
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
                continue

            # ── Step 3: Join MUTED with dummy WebRTC params ────────────
            ssrc = random.randint(10000, 99999999)
            ssrc_map[phone] = ssrc

            await client(JoinGroupCallRequest(
                call=call,
                join_as=await client.get_me(),
                params=DataJSON(data=json.dumps({
                    "ufrag": f"uf{ssrc}",
                    "pwd":   f"pw{ssrc}",
                    "fingerprints": [],
                    "ssrc": ssrc,
                    "ssrc-groups": [],
                    "payload-types": [],
                    "rtp-hdrexts": []
                })),
                muted=True,
                video_stopped=True,
            ))

            # ── Step 4: Register auto-remute handler ────────────────────
            me      = await client.get_me()
            handler = _make_remute_handler(client, call, me.id)
            client.add_event_handler(handler, events.Raw(UpdateGroupCallParticipants))
            live_handlers.setdefault(phone, []).append(handler)

            calls_map[phone] = call
            joined.append(phone)
            logging.info(f"[{phone}] ✅ Joined live MUTED")

        except Exception as e:
            logging.error(f"[{phone}] Live join failed: {e}")

    if not joined:
        await msg.edit_text(
            "❌ *Join করা সম্ভব হয়নি।*\n\n"
            "সম্ভাব্য কারণ:\n"
            "• Channel এ Active voice/live chat নেই\n"
            "• Link ভুল অথবা access নেই",
            parse_mode="Markdown",
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
        f"❌ Failed : *{count - len(joined)}* accounts",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 LIVE LEAVE", callback_data=f"leave:{chat_id}")
        ]])
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

def _make_remute_handler(client: TelegramClient, call, me_id: int):
    """
    Returns an event handler that automatically re-mutes this account
    if an admin unmutes it during a live/voice chat.
    """
    async def handler(ev):
        for p in ev.participants:
            uid = getattr(p.peer, 'user_id', None)
            if uid == me_id and not p.muted:
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

# ─── LIVE LEAVE CALLBACK ────────────────────────────────────────────────────────
async def live_leave_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.from_user.id != OWNER_ID:
        await q.answer("⛔ Only owner!", show_alert=True)
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
        call = session["calls"].get(phone)
        info = accs.get(phone)
        if not info or not call:
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
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
