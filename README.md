# 🤖 Telegram Live Join Bot

## ✅ Features
- Multiple Telegram account add/login (Telethon)
- Account list view
- Live join with selected number of accounts (joined **muted** automatically)
- Live leave with one button click
- Owner-only access
- Runs on Railway

---

## 📦 Setup Steps

### Step 1 — BotFather থেকে Bot Token নিন
1. Telegram এ @BotFather খুলুন
2. `/newbot` পাঠান
3. নাম ও username দিন
4. **Token** কপি করে রাখুন

### Step 2 — Telegram API credentials নিন
1. https://my.telegram.org এ যান
2. Login করুন → **API development tools** এ ক্লিক করুন
3. App তৈরি করুন → **api_id** ও **api_hash** কপি করুন

### Step 3 — Owner ID নিন
1. Telegram এ **@userinfobot** তে `/start` পাঠান
2. আপনার **User ID** নোট করুন

### Step 4 — Railway তে Deploy করুন
1. [railway.app](https://railway.app) এ যান → New Project
2. **Deploy from GitHub repo** বেছে নিন (অথবা "Deploy from local" দিয়ে এই folder আপলোড করুন)
3. **Variables** সেকশনে গিয়ে এই 4টি variable যোগ করুন:

| Variable   | Value                          |
|------------|-------------------------------|
| BOT_TOKEN  | আপনার bot token               |
| OWNER_ID   | আপনার telegram user ID        |
| API_ID     | my.telegram.org থেকে         |
| API_HASH   | my.telegram.org থেকে         |

4. Deploy করুন → **Worker** service হিসেবে চলবে

---

## 🚀 Bot ব্যবহার

### `/start`
Main menu আসবে তিনটি বাটন সহ:

```
[ ➕ ADD ACCOUNT ]  [ 📋 ACCOUNT LIST ]
       [ 🟢 JOIN ON LIVE ]
```

### ➕ Account Add
- নাম্বার দিন (+8801XXXXXXXXX)
- OTP কোড দিন
- যদি 2FA থাকে তাহলে password দিন
- ✅ Login Success!

### 📋 Account List
সব সেভ করা account দেখাবে নাম ও নাম্বার সহ।

### 🟢 Join On Live
1. Live link দিন (যেমন: `@channelname`)
2. কতটি account join করাবেন বলুন
3. সব account **muted** হয়ে join করবে
4. **🔴 LIVE LEAVE** বাটন চাপলে সব account leave করবে

---

## ⚠️ গুরুত্বপূর্ণ নোট
- শুধুমাত্র **Owner** ব্যবহার করতে পারবে
- Account data `accounts.json` ফাইলে সেভ হয় (Railway volume বা persistent storage ব্যবহার করুন)
- Live join করার পর admin unmute করলেও account নিজে থেকে **muted** থাকবে


## Live Join stability patch (muted mode)

This revision removes the fabricated WebRTC payload and the loop that sent a
new `JoinGroupCallRequest` every four seconds. It uses the `tgcalls` engine to
create the join payload and manages leaving through the engine.

Railway setup:
- Set `BOT_TOKEN`, `OWNER_ID`, `API_ID`, and `API_HASH` as Variables.
- Deploy from the repository root so Nixpacks reads `nixpacks.toml`.
- Keep the generated account/session file on persistent storage if you need
  sessions to survive redeploys. Railway's ephemeral filesystem can be reset.
- Check Deploy Logs if the native `tgcalls` dependency fails to install or a
  group call cannot be joined.

This patch has not been verified against your specific Telegram account,
target chat, or Railway deployment. Telegram can still reject a join or
disconnect a client; no code can guarantee that every account remains in a
call indefinitely.
