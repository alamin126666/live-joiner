# 🤖 Telegram Live Join Bot (PyTgCalls Edition)

## ✅ Features
- Multiple Telegram account add/login (Telethon)
- Account list view
- Live join with **real UDP stream** via PyTgCalls
- **Always থাকে** — কোনো ping বা loop নেই
- Admin unmute করলেও muted থাকে
- Live leave with one button click
- Owner-only access
- Runs on Railway

---

## 🔧 কেন PyTgCalls?

| আগে (Telethon only) | এখন (PyTgCalls) |
|---|---|
| Fake ping দিতে হতো | Real UDP stream |
| Telegram auto-remove করতো | সার্ভার মনে করে real user |
| বারবার request | একবার join, সবসময় থাকে |

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
2. **Deploy from GitHub repo** বেছে নিন
3. **Variables** সেকশনে গিয়ে এই 4টি variable যোগ করুন:

| Variable   | Value                          |
|------------|-------------------------------|
| BOT_TOKEN  | আপনার bot token               |
| OWNER_ID   | আপনার telegram user ID        |
| API_ID     | my.telegram.org থেকে         |
| API_HASH   | my.telegram.org থেকে         |

4. Deploy করুন → **Worker** service হিসেবে চলবে

> ⚠️ nixpacks.toml এ `ffmpeg` automatically install হবে।
> Bot প্রথমবার start হলে `silent.raw` file তৈরি করবে — এটা normal।

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
3. সব account real UDP stream দিয়ে join করবে
4. **🔴 LIVE LEAVE** বাটন চাপলে সব account leave করবে

---

## ⚠️ গুরুত্বপূর্ণ নোট
- শুধুমাত্র **Owner** ব্যবহার করতে পারবে
- Account data `accounts.json` ফাইলে সেভ হয়
- `silent.raw` file bot নিজেই তৈরি করে — manually করতে হবে না
- Live join করার পর admin unmute করলেও account **muted** থাকবে
