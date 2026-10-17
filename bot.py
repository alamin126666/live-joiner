import os
import asyncio
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.phone import CreateGroupCallRequest
from telethon.tl.types import InputChannel
import telethon.tl.types as tl_types

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
OWNER_ID    = int(os.environ.get("OWNER_ID", "0"))
API_ID      = int(os.environ.get("API_ID", "0"))
API_HASH    = os.environ.get("API_HASH", "")
ACCOUNTS_FILE = "accounts.json"

# ─── CONVERSATION STATES ──────────────────────────────────────────────────────
(
    MAIN_MENU,
    WAITING_PHONE,
    WAITING_CODE,
    WAITING_2FA,
    WAITING_LIVE_LINK,
    WAITING_ACCOUNT_COUNT,
) = range(6)

# ─── ACCOUNT STORAGE ──────────────────────────────────────────────────────────
def load_accounts() -> dict:
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_accounts(accounts: dict):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f, indent=2)

# In-memory session store during login flow
pending_clients: dict = {}   # phone -> TelegramClient (during login)
active_clients:  dict = {}   # phone -> TelegramClient (logged-in, ready)
live_join_state: dict = {}   # chat_id -> { "phones": [...], "live_link": ... }

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def owner_only(func):
    """Decorator – silently ignore non-owner messages."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else 0
        if user_id != OWNER_ID:
            await update.effective_message.reply_text("⛔ Only owner allowed.")
            return ConversationHandler.END
        return await func(update, ctx)
    return wrapper

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧"), KeyboardButton("📋 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗜𝗦𝗧")],
            [KeyboardButton("🟢 𝗝𝗢𝗜𝗡 𝗢𝗡 𝗟𝗜𝗩𝗘")],
        ],
        resize_keyboard=True,
    )

async def get_or_create_client(phone: str, session_str: str) -> TelegramClient:
    """Return a connected Telethon client for a saved account."""
    if phone in active_clients:
        client = active_clients[phone]
        if not client.is_connected():
            await client.connect()
        return client
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    active_clients[phone] = client
    return client

# ─── /start ───────────────────────────────────────────────────────────────────
@owner_only
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Account Manager Bot!*\n\nChoose an option below 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU

# ─── MAIN MENU ROUTER ─────────────────────────────────────────────────────────
@owner_only
async def main_menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "ADD ACCOUNT" in text:
        return await start_add_account(update, ctx)
    elif "ACCOUNT LIST" in text:
        return await show_account_list(update, ctx)
    elif "JOIN ON LIVE" in text:
        return await start_live_join(update, ctx)
    else:
        await update.message.reply_text("Please use the buttons below 👇", reply_markup=main_menu_keyboard())
        return MAIN_MENU

# ─── ADD ACCOUNT FLOW ─────────────────────────────────────────────────────────
async def start_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = load_accounts()
    total = len(accounts)
    await update.message.reply_text(
        f"📱 *ALREADY ACCOUNT : {total}*\n\n"
        "নতুন একাউন্ট এর নাম্বার দিন 👇\n\n"
        "_Example : +8801XXXXXXXXX_",
        parse_mode="Markdown",
    )
    return WAITING_PHONE

@owner_only
async def receive_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip().replace(" ", "")
    ctx.user_data["phone"] = phone

    accounts = load_accounts()
    if phone in accounts:
        await update.message.reply_text(
            "⚠️ এই নাম্বারটি আগেই যোগ করা আছে!\n\nMain menu তে ফিরে যাচ্ছি...",
            reply_markup=main_menu_keyboard(),
        )
        return MAIN_MENU

    # Start Telethon client
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    pending_clients[phone] = client

    try:
        result = await client.send_code_request(phone)
        ctx.user_data["phone_code_hash"] = result.phone_code_hash
        await update.message.reply_text(
            "✅ কোড পাঠানো হয়েছে!\n\n🔑 *ভেরিফিকেশন কোড দিন* 👇",
            parse_mode="Markdown",
        )
        return WAITING_CODE
    except FloodWaitError as e:
        await update.message.reply_text(
            f"⏳ Too many requests. {e.seconds} সেকেন্ড পরে চেষ্টা করুন।",
            reply_markup=main_menu_keyboard(),
        )
        await client.disconnect()
        del pending_clients[phone]
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: {e}\n\nMain menu তে ফিরে যাচ্ছি...",
            reply_markup=main_menu_keyboard(),
        )
        await client.disconnect()
        if phone in pending_clients:
            del pending_clients[phone]
        return MAIN_MENU

@owner_only
async def receive_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    phone = ctx.user_data.get("phone")
    client = pending_clients.get(phone)

    if not client:
        await update.message.reply_text("❌ Session expired. আবার চেষ্টা করুন।", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    try:
        await client.sign_in(phone, code, phone_code_hash=ctx.user_data["phone_code_hash"])
        # Success – no 2FA
        await _save_logged_in_account(update, ctx, client, phone)
        return MAIN_MENU

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 2FA Password প্রয়োজন। পাসওয়ার্ড দিন 👇",
            parse_mode="Markdown",
        )
        return WAITING_2FA

    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await update.message.reply_text(
            "❌ কোডটি ভুল বা মেয়াদ উত্তীর্ণ। আবার চেষ্টা করুন।",
            reply_markup=main_menu_keyboard(),
        )
        if phone in pending_clients:
            await client.disconnect()
            del pending_clients[phone]
        return MAIN_MENU

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_menu_keyboard())
        if phone in pending_clients:
            await client.disconnect()
            del pending_clients[phone]
        return MAIN_MENU

@owner_only
async def receive_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    phone = ctx.user_data.get("phone")
    client = pending_clients.get(phone)

    if not client:
        await update.message.reply_text("❌ Session expired.", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    try:
        await client.sign_in(password=password)
        await _save_logged_in_account(update, ctx, client, phone)
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ Wrong password: {e}", reply_markup=main_menu_keyboard())
        if phone in pending_clients:
            await client.disconnect()
            del pending_clients[phone]
        return MAIN_MENU

async def _save_logged_in_account(update, ctx, client, phone):
    session_str = client.session.save()
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone

    accounts = load_accounts()
    accounts[phone] = {"name": name, "session": session_str}
    save_accounts(accounts)

    active_clients[phone] = client
    if phone in pending_clients:
        del pending_clients[phone]

    await update.message.reply_text(
        f"✅ *Account Login Success!*\n\n"
        f"👤 Name: `{name}`\n"
        f"📱 Phone: `{phone}`",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )

# ─── ACCOUNT LIST ─────────────────────────────────────────────────────────────
async def show_account_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = load_accounts()
    if not accounts:
        await update.message.reply_text(
            "📋 কোনো অ্যাকাউন্ট নেই।\n\nপ্রথমে অ্যাকাউন্ট যোগ করুন।",
            reply_markup=main_menu_keyboard(),
        )
        return MAIN_MENU

    lines = [f"📋 *Total Account Count : {len(accounts)}*\n"]
    for i, (phone, info) in enumerate(accounts.items(), 1):
        lines.append(f"{i}. 👤 `{info['name']}` — `{phone}`")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU

# ─── LIVE JOIN FLOW ───────────────────────────────────────────────────────────
async def start_live_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    accounts = load_accounts()
    if not accounts:
        await update.message.reply_text(
            "❌ কোনো অ্যাকাউন্ট নেই। আগে অ্যাকাউন্ট যোগ করুন।",
            reply_markup=main_menu_keyboard(),
        )
        return MAIN_MENU

    await update.message.reply_text(
        "🎥 *Live Join*\n\nLive link বা username দিন 👇\n\n"
        "_Example: @channelname বা https://t.me/channelname_",
        parse_mode="Markdown",
    )
    return WAITING_LIVE_LINK

@owner_only
async def receive_live_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    ctx.user_data["live_link"] = link
    accounts = load_accounts()
    total = len(accounts)

    await update.message.reply_text(
        f"👥 মোট *{total}টি* অ্যাকাউন্ট আছে।\n\n"
        f"কতটি অ্যাকাউন্ট Live-এ Join করাতে চান? (1-{total})\n\n"
        f"_সব join করাতে চাইলে {total} লিখুন_",
        parse_mode="Markdown",
    )
    return WAITING_ACCOUNT_COUNT

@owner_only
async def receive_account_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    accounts = load_accounts()

    try:
        count = int(text)
        if count < 1 or count > len(accounts):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"⚠️ সঠিক সংখ্যা দিন (1 থেকে {len(accounts)})।"
        )
        return WAITING_ACCOUNT_COUNT

    live_link = ctx.user_data.get("live_link", "")
    selected_phones = list(accounts.keys())[:count]
    chat_id = update.effective_chat.id

    await update.message.reply_text(
        f"⏳ *{count}টি* অ্যাকাউন্ট দিয়ে Live Join করা হচ্ছে...\n\nঅনুগ্রহ করে অপেক্ষা করুন।",
        parse_mode="Markdown",
    )

    success_phones, fail_phones = await _do_live_join(live_link, selected_phones, accounts)

    if not success_phones:
        await update.message.reply_text(
            "❌ কোনো অ্যাকাউন্ট Join করতে পারেনি।",
            reply_markup=main_menu_keyboard(),
        )
        return MAIN_MENU

    live_join_state[chat_id] = {"phones": success_phones, "live_link": live_link}

    leave_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 LIVE LEAVE", callback_data=f"live_leave:{chat_id}")
    ]])

    fail_text = f"\n⚠️ Failed: {len(fail_phones)} accounts" if fail_phones else ""
    await update.message.reply_text(
        f"✅ *YOUR ACCOUNT LIVE JOIN SUCCESS* 🎉\n\n"
        f"✔️ Joined: *{len(success_phones)}* accounts{fail_text}\n\n"
        f"Live leave করতে নিচের বাটন চাপুন 👇",
        parse_mode="Markdown",
        reply_markup=leave_btn,
    )
    return MAIN_MENU

async def _do_live_join(live_link: str, phones: list, accounts: dict):
    """Join the given channel/group live for all selected phones."""
    success = []
    fail = []

    for phone in phones:
        info = accounts.get(phone)
        if not info:
            fail.append(phone)
            continue
        try:
            client = await get_or_create_client(phone, info["session"])
            entity = await client.get_entity(live_link)

            # Join if not already a member
            try:
                await client(JoinChannelRequest(entity))
            except Exception:
                pass  # Already a member or private – still try mute join

            # Mute self in voice chat (join muted)
            try:
                from telethon.tl.functions.phone import JoinGroupCallRequest
                from telethon.tl.types import DataJSON
                # Get the active call from the channel
                full = await client.get_entity(entity)
                # Attempt to join group call muted
                # Note: this requires the channel to have an active live/voice chat
                call_obj = getattr(full, 'call', None)
                if call_obj:
                    await client(JoinGroupCallRequest(
                        call=call_obj,
                        join_as=await client.get_me(),
                        params=DataJSON(data='{"fingerprints":[],"pwd":"","ufrag":"","ssrc":0,"ssrc-groups":[]}'),
                        muted=True,
                        video_stopped=True,
                    ))
            except Exception:
                pass  # Live not active or already joined

            success.append(phone)
        except Exception as e:
            print(f"[LIVE JOIN] {phone} failed: {e}")
            fail.append(phone)

    return success, fail

# ─── LIVE LEAVE CALLBACK ──────────────────────────────────────────────────────
async def live_leave_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id != OWNER_ID:
        await query.answer("⛔ Only owner allowed.", show_alert=True)
        return

    data = query.data  # "live_leave:<chat_id>"
    chat_id = int(data.split(":")[1])
    state = live_join_state.get(chat_id)

    if not state:
        await query.edit_message_text("⚠️ কোনো active live session নেই।")
        return

    phones    = state["phones"]
    live_link = state["live_link"]
    accounts  = load_accounts()

    await query.edit_message_text("⏳ সকল অ্যাকাউন্ট Live Leave করছে...")

    left = 0
    for phone in phones:
        info = accounts.get(phone)
        if not info:
            continue
        try:
            client = await get_or_create_client(phone, info["session"])
            entity = await client.get_entity(live_link)

            # Leave voice/group call
            try:
                from telethon.tl.functions.phone import LeaveGroupCallRequest
                full = await client.get_entity(entity)
                call_obj = getattr(full, 'call', None)
                if call_obj:
                    await client(LeaveGroupCallRequest(call=call_obj, source=0))
            except Exception:
                pass

            left += 1
        except Exception as e:
            print(f"[LIVE LEAVE] {phone} failed: {e}")

    del live_join_state[chat_id]

    await query.edit_message_text(
        f"✅ *{left}টি* অ্যাকাউন্ট Live Leave করেছে।",
        parse_mode="Markdown",
    )

# ─── CANCEL ───────────────────────────────────────────────────────────────────
@owner_only
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ বাতিল করা হয়েছে।", reply_markup=main_menu_keyboard())
    return MAIN_MENU

# ─── BUILD & RUN ──────────────────────────────────────────────────────────────
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            ],
            WAITING_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone),
            ],
            WAITING_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code),
            ],
            WAITING_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_2fa),
            ],
            WAITING_LIVE_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_live_link),
            ],
            WAITING_ACCOUNT_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_account_count),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(live_leave_callback, pattern=r"^live_leave:"))

    print("🤖 Bot started...")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print("✅ Polling started. Press Ctrl+C to stop.")
        await asyncio.Event().wait()  # Run forever until killed

if __name__ == "__main__":
    asyncio.run(main())
